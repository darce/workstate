from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from workstate_handoff_mcp import api, cli
from workstate_handoff_mcp.review_findings_updates import WorkspaceCleanliness


def _run_cli(argv: list[str], capsys) -> dict:
    # WORKSTATE-REF-17-10 implementation note introduced ConsumerRootResolutionError in
    # RuntimeConfig.from_args() when --workspace-root resolves to a path
    # outside any git repo AND no explicit state/output overrides are
    # passed. Test fixtures use pytest tmp_path (non-git) so we
    # auto-inject --state-dir <workspace>/.task-state when the caller
    # supplied --workspace-root without --state-dir. Production callers
    # still must set state-dir or live inside a git repo.
    if "--workspace-root" in argv and "--state-dir" not in argv:
        ws_idx = argv.index("--workspace-root")
        if ws_idx + 1 < len(argv):
            ws_path = Path(argv[ws_idx + 1])
            argv = list(argv)
            argv.insert(ws_idx + 2, str(ws_path / ".task-state"))
            argv.insert(ws_idx + 2, "--state-dir")
    original_argv = sys.argv
    sys.argv = argv
    try:
        cli.main()
    finally:
        sys.argv = original_argv
    return _parse_response(capsys.readouterr().out)


def _parse_response(raw: str | dict) -> dict:
    """Convenience accessor (WORKSTATE-REF-10): handlers now return dicts natively.

    This helper accepts:
      - native ``dict`` returned by an MCP handler call (the new path)
      - ``str`` JSON output from CLI stdout capture
      - ``str`` JSON pulled from a stored DB column (e.g.
        ``decision["changed_files_json"]``); the parsed value may itself be
        a list, in which case the v2-envelope merge below short-circuits.

    The previous WORKSTATE-REF-7 helper assumed string-only handler returns; the
    WORKSTATE-REF-10 dict-return refactor flipped handler signatures to ``-> dict``
    and we route every former ``json.loads(...)`` call site through this
    helper to handle both shapes uniformly.
    """
    result = raw if isinstance(raw, dict) else json.loads(raw)
    if isinstance(result, dict) and result.get("schema_version") == 2:
        data = result.get("data", {})
        scope = result.get("scope", {})
        flat = {**result, **data}
        if "task_ref" not in flat and scope.get("task_ref"):
            flat["task_ref"] = scope["task_ref"]
        return flat
    return result


def test_doctor_cli_reports_workspace_paths(tmp_path: Path, capsys) -> None:
    # WORKSTATE-REF-17-10 implementation note: doctor spawns subprocess CLI probes that pass only
    # --workspace-root. Initialize tmp_path as a git repo so those
    # subprocesses pass the ConsumerRootResolutionError check. (The
    # outer _run_cli helper auto-injects --state-dir, but the doctor's
    # internal subprocess invocations do not.)
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "doctor",
        ],
        capsys,
    )

    assert payload["ok"] is True
    assert payload["workspace_root"] == str(tmp_path.resolve())


def test_init_state_cli_bootstraps_workspace_paths(tmp_path: Path, capsys) -> None:
    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "init-state",
        ],
        capsys,
    )

    assert payload["ok"] is True
    assert payload["state_dir"] == str((tmp_path / ".task-state").resolve())
    assert payload["exports_dir"] == str((tmp_path / ".task-state" / "exports").resolve())
    assert payload["db_path"] == str((tmp_path / ".task-state" / "handoff.db").resolve())
    assert payload["schema_version"] > 0


def test_init_state_check_cli_reports_uninitialized_without_creating_state(tmp_path: Path, capsys) -> None:
    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "init-state",
            "--check",
        ],
        capsys,
    )

    assert payload["ok"] is True
    assert payload["initialized"] is False
    assert payload["state_dir"] == str((tmp_path / ".task-state").resolve())
    assert payload["exports_dir"] == str((tmp_path / ".task-state" / "exports").resolve())
    assert payload["db_path"] == str((tmp_path / ".task-state" / "handoff.db").resolve())
    assert payload["schema_version"] is None
    assert not (tmp_path / ".task-state").exists()


def test_init_state_cli_refuses_foreign_state_without_force_reuse_state(tmp_path: Path, capsys) -> None:
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True)
    with sqlite3.connect(state_dir / "handoff.db"):
        pass

    with pytest.raises(RuntimeError, match="force-reuse-state"):
        _run_cli(
            [
                "mcp-workstate-handoff",
                "--workspace-root",
                str(tmp_path),
                "init-state",
            ],
            capsys,
        )


def test_init_state_cli_force_reuse_state_allows_existing_db_without_manifest(tmp_path: Path, capsys) -> None:
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True)
    with sqlite3.connect(state_dir / "handoff.db") as conn:
        conn.execute("PRAGMA user_version = 7")

    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "init-state",
            "--force-reuse-state",
        ],
        capsys,
    )

    assert payload["ok"] is True
    assert payload["initialized"] is True
    assert payload["db_created"] is False
    assert payload["force_reuse_state"] is True


def test_init_state_cli_rejects_manifest_with_mismatched_expected_remote_url(
    tmp_path: Path,
    capsys,
) -> None:
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True)
    with sqlite3.connect(state_dir / "handoff.db"):
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
        _run_cli(
            [
                "mcp-workstate-handoff",
                "--workspace-root",
                str(tmp_path),
                "init-state",
                "--expected-remote-url",
                "git@example.com:other/repo.git",
            ],
            capsys,
        )


def test_init_state_cli_honors_state_dir_env_var(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "custom-state"
    monkeypatch.setenv("AGENT_HANDOFF_STATE_DIR", str(state_dir))

    original_argv = sys.argv
    sys.argv = [
        "mcp-workstate-handoff",
        "--workspace-root",
        str(tmp_path),
        "init-state",
    ]
    try:
        cli.main()
    finally:
        sys.argv = original_argv

    payload = _parse_response(capsys.readouterr().out)

    assert payload["ok"] is True
    assert payload["state_dir"] == str(state_dir.resolve())
    assert payload["db_path"] == str((state_dir / "handoff.db").resolve())
    assert not (tmp_path / ".task-state").exists()


def test_state_review_list_and_close_check_cli_smoke(tmp_path: Path, capsys) -> None:
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(api.set_handoff_state(task_ref="task-1", objective="cli smoke"))
    _parse_response(
        api.record_review_finding(
            session="cli",
            finding_id="M-1",
            severity="medium",
            file_path="README.md",
            description="cli review list smoke",
        )
    )

    state_payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "state",
        ],
        capsys,
    )
    assert state_payload["ok"] is True
    assert state_payload["task_ref"] == "task-1"

    findings_payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "review-findings",
            "--operation",
            "list",
        ],
        capsys,
    )
    assert findings_payload["ok"] is True
    assert findings_payload["total_matching"] == 1

    close_payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "integrity-check",
            "--kind",
            "close",
        ],
        capsys,
    )
    assert close_payload["ok"] is True
    assert close_payload["ready_to_close"] is False


def test_state_cli_sections_flag(tmp_path: Path, capsys) -> None:
    """--sections limits which data sections appear in CLI output."""
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(api.set_handoff_state(task_ref="sec-cli", objective="sections cli smoke"))
    _parse_response(api.record_decision(session="s1", decision="d1"))
    _parse_response(api.report_blocker(operation="add", description="b1"))

    payload = _run_cli(
        ["mcp-workstate-handoff", "--workspace-root", str(tmp_path), "state", "--sections", "decisions_recent"],
        capsys,
    )
    assert payload["ok"] is True
    assert "active" in payload
    assert "limits" in payload
    assert "decisions_recent" in payload
    assert "blockers_open" not in payload

    # Identity-only: explicit 'identity' token → only active + limits
    identity = _run_cli(
        ["mcp-workstate-handoff", "--workspace-root", str(tmp_path), "state", "--sections", "identity"],
        capsys,
    )
    assert identity["ok"] is True
    assert "active" in identity
    assert "limits" in identity
    assert "blockers_open" not in identity
    assert "decisions_recent" not in identity


def test_state_cli_decision_filter_and_fields_flags(tmp_path: Path, capsys) -> None:
    """--decision-branch filters decisions_recent; --decision-fields narrows the projection."""
    from workstate_handoff_mcp import core as handoff_core

    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(api.set_handoff_state(task_ref="dec-cli", objective="decision cli smoke"))
    handoff_core.record_decision(
        session="s1",
        decision="cdx_decision_alpha",
        actor=handoff_core.WriteActor(
            agent="codex",
            branch="feature/branch-a",
            commit_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            lane_id="lane-a",
        ),
    )
    handoff_core.record_decision(
        session="s1",
        decision="cdx_decision_beta",
        actor=handoff_core.WriteActor(
            agent="codex",
            branch="feature/branch-b",
            commit_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            lane_id="lane-b",
        ),
    )

    filtered = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "state",
            "--sections",
            "decisions_recent",
            "--decision-branch",
            "feature/branch-a",
            "--decision-fields",
            "decision",
            "branch",
        ],
        capsys,
    )
    assert filtered["ok"] is True
    rows = filtered["decisions_recent"]
    assert rows and all(r["branch"] == "feature/branch-a" for r in rows)
    assert all(set(r.keys()) == {"decision", "branch"} for r in rows)


def test_state_cli_detail_flag(tmp_path: Path, capsys) -> None:
    """--detail summary truncates long fields via CLI."""
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(api.set_handoff_state(task_ref="det-cli", objective="detail cli smoke"))
    _parse_response(api.record_decision(session="s1", decision="d1", rationale="R" * 500))

    full = _run_cli(
        ["mcp-workstate-handoff", "--workspace-root", str(tmp_path), "state", "--detail", "full"],
        capsys,
    )
    assert len(full["decisions_recent"][0]["rationale"]) == 500

    summary = _run_cli(
        ["mcp-workstate-handoff", "--workspace-root", str(tmp_path), "state", "--detail", "summary"],
        capsys,
    )
    assert summary["decisions_recent"][0]["rationale"].endswith("...")
    assert len(summary["decisions_recent"][0]["rationale"]) == 203


def test_validate_decision_id_cli_surfaces_suggestion(tmp_path: Path, capsys) -> None:
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))

    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "validate",
            "--kind",
            "decision_id",
            "--decision",
            "codex_slice_complete_plan0004_contract-pinning-and-docs",
            "--decision-kind",
            "slice_complete",
        ],
        capsys,
    )

    assert payload["ok"] is False
    assert payload["category"] == "malformed_slice"
    assert payload["suggested"] == "codex_slice_complete_plan0004_contract_pinning_and_docs"


def test_review_list_cli_detail_flag(tmp_path: Path, capsys) -> None:
    """review-findings --operation list --detail summary truncates long fields via CLI."""
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(api.set_handoff_state(task_ref="rl-det", objective="review-list detail smoke"))
    _parse_response(
        api.record_review_finding(
            session="cli",
            finding_id="M-1",
            severity="medium",
            file_path="README.md",
            description="D" * 500,
        )
    )

    full = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "review-findings",
            "--operation",
            "list",
            "--detail",
            "full",
        ],
        capsys,
    )
    assert len(full["findings"][0]["description"]) == 500

    summary = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "review-findings",
            "--operation",
            "list",
            "--detail",
            "summary",
        ],
        capsys,
    )
    assert summary["findings"][0]["description"].endswith("...")
    assert len(summary["findings"][0]["description"]) == 203


def test_review_findings_cli_integrate_round_trips_typed_surface(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKSTATE-REF-68 implementation note: ``review-findings --operation integrate --integration-ref main``
    routes through the typed ``review_findings`` MCP surface and promotes a
    ``resolved_on_branch`` row whose anchor commit is reachable from the
    integration ref."""
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(
        api.set_handoff_state(
            task_ref="cli-int",
            objective="cli integrate roundtrip",
            target_branch="feature/cli-int",
        )
    )

    from workstate_handoff_mcp import review_findings_updates as rfu
    from workstate_handoff_mcp.shared_schema import _get_db_connection

    anchor = "9" * 40
    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO review_findings (
                finding_id, task_ref, severity, status, file_path, description, session,
                resolved_on_branch_at_commit, resolved_on_branch_ref, resolved_on_branch_at_ts
            ) VALUES (?, ?, 'medium', 'resolved_on_branch', 'cli.py', 'cli integrate', 's-cli',
                      ?, 'feature/cli-int', datetime('now'))
            """,
            ("CLI-INT-001", "cli-int", anchor),
        )

    integration_head = "a" * 40
    monkeypatch.setattr(rfu, "_resolve_integration_ref_head_sha", lambda ref: integration_head)
    monkeypatch.setattr(rfu, "_is_ancestor_of_ref", lambda candidate, ref: True)

    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "review-findings",
            "--operation",
            "integrate",
            "--task-ref",
            "cli-int",
            "--integration-ref",
            "main",
        ],
        capsys,
    )

    assert payload.get("ok") is True, payload
    promoted_ids = [item["finding_id"] for item in payload.get("promoted", [])]
    assert "CLI-INT-001" in promoted_ids
    with _get_db_connection() as conn:
        row = conn.execute(
            "SELECT status, integrated_at_commit, integrated_at_ref FROM review_findings"
            " WHERE finding_id = 'CLI-INT-001' AND task_ref = 'cli-int'"
        ).fetchone()
    assert row is not None
    assert row["status"] == "integrated"
    assert row["integrated_at_commit"] == integration_head
    assert row["integrated_at_ref"] == "main"


def test_review_runs_cli_rejects_branch_diff_subject_kind(tmp_path: Path, capsys) -> None:
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(api.set_handoff_state(task_ref="cli-subject-kind", objective="subject kind cli"))

    with pytest.raises(SystemExit) as exc:
        _run_cli(
            [
                "mcp-workstate-handoff",
                "--workspace-root",
                str(tmp_path),
                "review-runs",
                "--operation",
                "record",
                "--task-ref",
                "cli-subject-kind",
                "--review-run-id",
                "cli-subject-kind-run",
                "--session",
                "cli",
                "--subject-path",
                "main...HEAD",
                "--subject-kind",
                "branch_diff",
            ],
            capsys,
        )

    assert exc.value.code == 2
    assert "invalid choice: 'branch_diff'" in capsys.readouterr().err


def test_artifact_search_cli_fields_flag(tmp_path: Path, capsys) -> None:
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(api.set_handoff_state(task_ref="artifact-search-cli", objective="artifact search cli"))
    _parse_response(
        api.record_artifact(
            task_ref="artifact-search-cli",
            source_kind="log",
            source_label="artifact-search-log",
            content="column missing\n" * 120,
            summary="backend artifact search summary",
        )
    )

    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "artifacts",
            "--operation",
            "search",
            "--query",
            "column missing",
            "--fields",
            "source_id,title,snippet",
        ],
        capsys,
    )

    assert payload["ok"] is True
    assert payload["hits"]
    assert set(payload["hits"][0]) <= {"source_id", "title", "snippet"}


def test_artifact_list_cli_fields_flag(tmp_path: Path, capsys) -> None:
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(api.set_handoff_state(task_ref="artifact-list-cli", objective="artifact list cli"))
    _parse_response(
        api.record_artifact(
            task_ref="artifact-list-cli",
            source_kind="log",
            source_label="artifact-list-log",
            content="list payload\n" * 120,
            summary="artifact list summary",
        )
    )

    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "artifact-list",
            "--task-ref",
            "artifact-list-cli",
            "--fields",
            "source_label,summary",
        ],
        capsys,
    )

    assert payload["ok"] is True
    assert payload["sources"]
    assert set(payload["sources"][0]) <= {"source_label", "summary"}


def test_artifact_get_cli_detail_and_fields_flags(tmp_path: Path, capsys) -> None:
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(api.set_handoff_state(task_ref="artifact-get-cli", objective="artifact get cli"))
    recorded = _parse_response(
        api.record_artifact(
            task_ref="artifact-get-cli",
            source_kind="doc",
            source_label="artifact-get-doc",
            content=("chunk body\n" * 200),
        )
    )

    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "artifacts",
            "--operation",
            "get",
            "--source-id",
            str(recorded["source_id"]),
            "--detail",
            "summary",
            "--fields",
            "source_label,chunk_count",
        ],
        capsys,
    )

    assert payload["ok"] is True
    assert set(payload["source"]) <= {"source_label", "chunk_count"}
    assert payload["source"]["source_label"] == "artifact-get-doc"


def test_handoff_search_cli_fields_flag(tmp_path: Path, capsys) -> None:
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(api.set_handoff_state(task_ref="handoff-search-cli", objective="handoff search cli"))
    _parse_response(api.record_decision(session="cli", decision="handoff search keyword"))

    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "handoff-search",
            "--query",
            "handoff search",
            "--fields",
            "record_type,snippet",
        ],
        capsys,
    )

    assert payload["ok"] is True
    assert payload["results"]
    assert set(payload["results"][0]) <= {"record_type", "snippet"}


def test_handoff_search_cli_decision_fields_flag(tmp_path: Path, capsys) -> None:
    """handoff-search --decision-fields exposes the implementation note decision projection over the CLI."""
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(api.set_handoff_state(task_ref="handoff-search-decision-cli", objective="cli decision projection"))
    actor = api.WriteActor(
        agent="codex",
        branch="feature/WORKSTATE-36-decision-read-surface-parameterization",
        commit_sha="0000000000000000000000000000000000000000",
        lane_id="WORKSTATE-36",
    )
    _parse_response(
        api.record_decision(
            session="cli",
            decision="cli-decision-fields-keyword decision body",
            actor=actor,
        )
    )

    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "handoff-search",
            "--query",
            "cli-decision-fields-keyword",
            "--record-types",
            "decision",
            "--decision-fields",
            "decision",
            "branch",
            "commit_sha",
        ],
        capsys,
    )

    assert payload["ok"] is True
    assert payload["results"]
    row = payload["results"][0]
    assert row["record_type"] == "decision"
    assert row["decision"] == "cli-decision-fields-keyword decision body"
    assert row["branch"] == "feature/WORKSTATE-36-decision-read-surface-parameterization"
    assert row["commit_sha"] == "0000000000000000000000000000000000000000"


def test_event_cli_decision_variant_persists_changed_files(tmp_path: Path, capsys) -> None:
    """event --event-kind decision persists structured scope metadata."""
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(api.set_handoff_state(task_ref="dec-cli", objective="decision cli changed files"))

    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "event",
            "--event-kind",
            "decision",
            "--session",
            "cli",
            "--decision",
            "cop_slice_complete_decision_cli_changed_files",
            "--rationale",
            "## Changes\n- cli.\n## Verification\n- tested.\n## Schema / Contract Changes\n- none.\n## Open Threads\n- none.",
            "--changed-files",
            "packages/mcp-workstate-handoff/src/workstate_handoff_mcp/decisions.py",
            "packages/mcp-workstate-handoff/tests/test_cli.py",
        ],
        capsys,
    )

    assert payload["ok"] is True
    assert _parse_response(payload["decision"]["changed_files_json"]) == [
        "packages/mcp-workstate-handoff/src/workstate_handoff_mcp/decisions.py",
        "packages/mcp-workstate-handoff/tests/test_cli.py",
    ]


def test_review_update_cli_accepts_explicit_task_ref(tmp_path: Path, capsys) -> None:
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(api.set_handoff_state(task_ref="task-a", objective="task a"))
    _parse_response(
        api.record_review_finding(
            session="cli",
            finding_id="M-9",
            severity="medium",
            file_path="README.md",
            description="cross-task cli update",
        )
    )
    _parse_response(api.set_handoff_state(task_ref="task-b", objective="task b", expected_revision=0))

    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "review-findings",
            "--operation",
            "update",
            "--finding-id",
            "M-9",
            "--status",
            "fixed",
            "--task-ref",
            "task-a",
        ],
        capsys,
    )

    assert payload["ok"] is True
    assert payload["finding"]["task_ref"] == "task-a"
    assert payload["finding"]["status"] == "resolved_on_branch"


def test_review_update_cli_accepts_verified_commit_sha(tmp_path: Path, capsys, monkeypatch) -> None:
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(api.set_handoff_state(task_ref="task-a", objective="task a"))
    _parse_response(
        api.record_review_finding(
            session="cli",
            finding_id="M-10",
            severity="medium",
            file_path="README.md",
            description="descendant verification",
            actor={"agent": "reviewer", "branch": "feature/review", "commit_sha": "abc123"},
        )
    )

    from workstate_handoff_mcp import core as handoff_core

    monkeypatch.setattr(handoff_core, "_detect_git_write_context", lambda: ("feature/review", "def456"))
    monkeypatch.setattr(
        handoff_core,
        "_classify_commit_relation",
        lambda reference_sha, candidate_sha: (
            "descendant" if (reference_sha, candidate_sha) == ("abc123", "def456") else "same"
        ),
    )

    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "review-findings",
            "--operation",
            "update",
            "--finding-id",
            "M-10",
            "--status",
            "fixed",
            "--resolution-notes",
            "Verified on descendant commit def456.",
            "--verified-commit-sha",
            "def456",
            "--task-ref",
            "task-a",
        ],
        capsys,
    )

    assert payload["ok"] is True
    assert payload["finding"]["status"] == "resolved_on_branch"
    assert payload["commit_guard"]["verified_commit_sha"] == "def456"


def test_review_resolve_cli_smoke_returns_resolution_receipt(tmp_path: Path, capsys, monkeypatch) -> None:
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(api.set_handoff_state(task_ref="task-a", objective="task a"))
    _parse_response(
        api.record_review_finding(
            session="cli",
            finding_id="M-11",
            severity="medium",
            file_path="README.md",
            description="cli resolve smoke",
            actor={"agent": "reviewer", "commit_sha": "abc123"},
        )
    )
    from workstate_handoff_mcp import core as handoff_core

    monkeypatch.setattr(handoff_core, "_detect_git_write_context", lambda: ("feature/review", "abc123"))
    monkeypatch.setattr(
        "workstate_handoff_mcp.review_findings_updates._workspace_has_uncommitted_changes",
        lambda *a, **k: WorkspaceCleanliness(False),
    )
    monkeypatch.setattr(
        "workstate_handoff_mcp.review_findings_updates._classify_commit_relation",
        lambda reference_sha, candidate_sha: (
            "same" if (reference_sha, candidate_sha) == ("abc123", "abc123") else "unknown"
        ),
    )

    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "review-findings",
            "--operation",
            "resolve",
            "--task-ref",
            "task-a",
            "--resolve-finding-id",
            "M-11",
            "--session",
            "cli-resolve",
        ],
        capsys,
    )

    assert payload["ok"] is True
    assert payload["receipt"]["session"] == "cli-resolve"
    assert payload["receipt"]["counts"]["fixed"] == 1
    assert payload["receipt"]["results"][0]["finding_id"] == "M-11"
    assert payload["receipt"]["results"][0]["outcome"] == "fixed"


def test_validate_write_cli_surfaces_payload_errors(tmp_path: Path, capsys) -> None:
    """The consolidated ``validate --kind write`` CLI surface routes write-contract preflights.

    The branch-review skill documents the CLI wrapper as the fallback MCP
    surface when the stdio server is unavailable. WORKSTATE-REF-45 implementation note collapsed
    ``validate-decision-id`` and ``validate-write`` into ``validate
    --kind=...``; this test pins write-payload preflight via the merged
    surface.
    """

    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))

    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "validate",
            "--kind",
            "write",
            "--tool-name",
            "review_findings",
            "--payload-json",
            json.dumps({"operation": "record", "session": "cli", "finding_id": "X-1"}),
        ],
        capsys,
    )

    assert payload["tool_name"] == "review_findings"
    assert payload["ok"] is False
    assert isinstance(payload["errors"], list)
    assert payload["errors"], "missing-required-field errors expected"


def test_set_cli_explicit_commit_sha_overrides_stored_target(tmp_path: Path, capsys) -> None:
    """`set --commit-sha <sha> --branch <branch>` writes the explicit values into
    `updated_commit_sha`/`updated_branch` regardless of the row's stored
    target_worktree_path or the process cwd.

    WORKSTATE-REF-51 implementation note: callers that already know the commit they want to project
    (notably `make slice-commit`) need a way to bypass the resolver's
    stored-row fallback. Mirrors the actor-block plumbing that
    `event --event-kind …` already exposes.
    """
    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    # Seed the row pointing at /tmp/elsewhere so the resolver's task_git path
    # cannot succeed (path doesn't exist). Without --commit-sha the projector
    # would fall back to git_cwd and write whatever HEAD the test happens to
    # be running at.
    foreign_path = str(tmp_path / "elsewhere")
    _parse_response(
        api.set_handoff_state(
            task_ref="explicit-sha-cli",
            objective="explicit commit sha smoke",
            target_branch="feature/explicit-sha",
            target_worktree_path=foreign_path,
        )
    )

    explicit_sha = "deadbeefcafef00d1234567890abcdef12345678"
    explicit_branch = "feature/explicit-sha"

    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "set",
            "--task-ref",
            "explicit-sha-cli",
            "--focus",
            "explicit commit channel",
            "--expected-revision",
            "0",
            "--commit-sha",
            explicit_sha,
            "--branch",
            explicit_branch,
        ],
        capsys,
    )

    assert payload["ok"] is True
    active = payload["active"]
    assert active["updated_commit_sha"] == explicit_sha
    assert active["updated_branch"] == explicit_branch


def test_serve_http_parser_defaults() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(["serve-http"])
    assert args.subcommand == "serve-http"
    assert args.host == "127.0.0.1"
    assert args.port == 8741


def test_serve_http_parser_custom_host_port() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(["serve-http", "--host", "0.0.0.0", "--port", "9999"])
    assert args.host == "0.0.0.0"
    assert args.port == 9999


# ---------------------------------------------------------------------------
# WORKSTATE-REF-82 implementation note — main CLI parser exposes --dashboard-path (parity with
# sibling CLIs) and `python -m workstate_handoff_mcp.cli` is non-silent.
# ---------------------------------------------------------------------------


def test_cli_dashboard_path_flag_sets_runtime_config(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    dash = tmp_path / "custom" / "DASH.txt"

    parser = cli._build_parser()
    args = parser.parse_args(
        [
            "--workspace-root",
            str(tmp_path),
            "--state-dir",
            str(tmp_path / ".task-state"),
            "--dashboard-path",
            str(dash),
            "doctor",
        ]
    )

    assert args.dashboard_path == str(dash)
    config = api.RuntimeConfig.from_args(args)
    assert config.dashboard_path == dash.resolve()


def test_cli_module_invocation_is_non_silent() -> None:
    """`python -m workstate_handoff_mcp.cli --version` must run main(), not return silently.

    Without an `if __name__ == "__main__"` guard the module imports and exits 0
    with no output; the version assertions below fail in that case.
    """
    import workstate_handoff_mcp

    proc = subprocess.run(
        [sys.executable, "-m", "workstate_handoff_mcp.cli", "--version"],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0
    output = proc.stdout + proc.stderr
    assert "mcp-workstate-handoff" in output
    assert workstate_handoff_mcp.__version__ in output


# ---------------------------------------------------------------------------
# WORKSTATE-REF-47 implementation note — package __version__ + --version CLI flag
# ---------------------------------------------------------------------------


def test_package_version_constant_is_nonempty() -> None:
    import workstate_handoff_mcp

    assert isinstance(workstate_handoff_mcp.__version__, str)
    assert workstate_handoff_mcp.__version__


def test_package_version_constant_matches_pyproject() -> None:
    import tomllib

    import workstate_handoff_mcp

    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)
    assert workstate_handoff_mcp.__version__ == data["project"]["version"]


def test_cli_version_flag_prints_package_name_and_version_then_exits_zero(
    capsys,
) -> None:
    import workstate_handoff_mcp

    parser = cli._build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--version"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "mcp-workstate-handoff" in output
    assert workstate_handoff_mcp.__version__ in output


# ---------------------------------------------------------------------------
# WORKSTATE-REF-47 implementation note — run_doctor exposes top-level `version` key
# ---------------------------------------------------------------------------


def test_run_doctor_includes_top_level_version_key(tmp_path: Path) -> None:
    import workstate_handoff_mcp

    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    config = api.RuntimeConfig.for_workspace(tmp_path)
    result = api.run_doctor(config)

    assert result["version"] == workstate_handoff_mcp.__version__
    assert isinstance(result["version"], str)
    assert result["version"]


def test_run_doctor_preserves_existing_top_level_keys(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    config = api.RuntimeConfig.for_workspace(tmp_path)
    result = api.run_doctor(config)

    for key in (
        "ok",
        "workspace_root",
        "state_dir",
        "db_path",
        "artifact_db_path",
        "current_task_path",
        "exports_dir",
        "checks",
        "portable_hook_semantics",
    ):
        assert key in result, f"run_doctor lost top-level key {key!r}"


def test_run_doctor_does_not_introduce_data_wrapper(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    config = api.RuntimeConfig.for_workspace(tmp_path)
    result = api.run_doctor(config)

    assert "data" not in result, (
        "run_doctor must remain a raw top-level CLI dict; the new `version` "
        "key sits at the top level alongside ok/workspace_root/checks/etc., "
        "no `data` wrapper introduced."
    )
