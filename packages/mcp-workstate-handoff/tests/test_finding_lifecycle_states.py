"""WORKSTATE-REF-68 end-to-end lifecycle suite.

implementation note lands the schema migration (v9 -> v10) that adds the
`resolved_on_branch_at_*` / `integrated_at_*` columns to ``review_findings``,
adds the `last_observed_integration_sha` column to ``handoff_state``, and
expands the ``review_findings.status`` CHECK constraint to permit the new
``resolved_on_branch`` and ``integrated`` values. The same slice wires the
write path so a successful close persists ``resolved_on_branch_at_commit``
even with the feature flag off — the persistence anchor that ``FLS-PLAN-01``
demanded.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig
from workstate_handoff_mcp.review_findings_updates import WorkspaceCleanliness
from workstate_handoff_mcp.shared_schema import (
    HANDOFF_SCHEMA_VERSION,
    _get_db_connection,
)


def _parse(raw: str | dict) -> dict:
    result = raw if isinstance(raw, dict) else json.loads(raw)
    if isinstance(result, dict) and result.get("schema_version") == 2:
        data = result.get("data", {})
        scope = result.get("scope", {})
        flat = {**result, **data}
        if "task_ref" not in flat and scope.get("task_ref"):
            flat["task_ref"] = scope["task_ref"]
        return flat
    return result


def _mark_workspace_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "workstate_handoff_mcp.review_findings_updates._workspace_has_uncommitted_changes",
        lambda: WorkspaceCleanliness(False),
    )


@pytest.fixture()
def isolated_handoff(tmp_path: Path):
    state_dir = tmp_path / ".task-state"
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
        dashboard_path=tmp_path / "DASHBOARD.md",
        current_task_auto_regen=True,
    )
    mcp_server.configure_runtime(runtime)
    return {"state_dir": state_dir, "db_path": runtime.db_path}


@pytest.fixture()
def isolated_handoff_flag_off(tmp_path: Path):
    """WORKSTATE-REF-68 implementation note: counterpart to ``isolated_handoff_flag_on`` for
    legacy regression coverage now that the default flipped to ``True``.

    Tests that need the pre-Slice-5c single-state behavior (``fixed`` close
    leaves ``status='fixed'`` on the row) opt in via this fixture."""
    state_dir = tmp_path / ".task-state"
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
        dashboard_path=tmp_path / "DASHBOARD.md",
        current_task_auto_regen=True,
        finding_lifecycle_states_enabled=False,
    )
    mcp_server.configure_runtime(runtime)
    return {"state_dir": state_dir, "db_path": runtime.db_path}


@pytest.fixture()
def isolated_handoff_flag_on(tmp_path: Path):
    """Same as ``isolated_handoff`` but with the WORKSTATE-REF-68 lifecycle flag on
    so write paths flip ``status='fixed'`` to ``status='resolved_on_branch'``
    for implementation note behavior coverage."""
    state_dir = tmp_path / ".task-state"
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
        dashboard_path=tmp_path / "DASHBOARD.md",
        current_task_auto_regen=True,
        finding_lifecycle_states_enabled=True,
    )
    mcp_server.configure_runtime(runtime)
    return {"state_dir": state_dir, "db_path": runtime.db_path}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ---------------------------------------------------------------------------
# implementation note: schema migration
# ---------------------------------------------------------------------------


def test_schema_version_at_least_v11(isolated_handoff: dict) -> None:
    """Cold-start bootstrap is at or past the WORKSTATE-REF-68 schema sentinel."""
    assert HANDOFF_SCHEMA_VERSION >= 11
    with _get_db_connection() as conn:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        assert user_version == HANDOFF_SCHEMA_VERSION


def test_review_findings_has_resolved_on_branch_columns(isolated_handoff: dict) -> None:
    """The three resolved_on_branch_at_* columns land on review_findings."""
    with _get_db_connection() as conn:
        cols = _table_columns(conn, "review_findings")
    for column in (
        "resolved_on_branch_at_commit",
        "resolved_on_branch_ref",
        "resolved_on_branch_at_ts",
    ):
        assert column in cols, f"review_findings.{column} must exist after v10 migration"


def test_review_findings_has_integrated_columns(isolated_handoff: dict) -> None:
    """The three integrated_at_* columns land on review_findings."""
    with _get_db_connection() as conn:
        cols = _table_columns(conn, "review_findings")
    for column in ("integrated_at_commit", "integrated_at_ref", "integrated_at_ts"):
        assert column in cols, f"review_findings.{column} must exist after v10 migration"


def test_handoff_state_has_last_observed_integration_sha(isolated_handoff: dict) -> None:
    """handoff_state grows the integrate-debounce column."""
    with _get_db_connection() as conn:
        cols = _table_columns(conn, "handoff_state")
    assert "last_observed_integration_sha" in cols


def test_status_check_accepts_resolved_on_branch_and_integrated(isolated_handoff: dict) -> None:
    """Direct SQL inserts with the new statuses must succeed after v10."""
    with _get_db_connection() as conn:
        for status in ("resolved_on_branch", "integrated"):
            conn.execute(
                """
                INSERT INTO review_findings (
                    finding_id, task_ref, severity, status, file_path, description, session
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (f"CHECK-{status}", "schema-check-task", "low", status, "f.py", "check", "s1"),
            )
        rows = conn.execute(
            "SELECT finding_id, status FROM review_findings WHERE task_ref = 'schema-check-task' ORDER BY id"
        ).fetchall()
    statuses = {str(row["status"]) for row in rows}
    assert statuses == {"resolved_on_branch", "integrated"}


def test_status_check_still_rejects_unknown_values(isolated_handoff: dict) -> None:
    """v10's expanded CHECK must not become a free-for-all."""
    with _get_db_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO review_findings (
                    finding_id, task_ref, severity, status, file_path, description, session
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("CHECK-BAD", "schema-check-task", "low", "garbage", "f.py", "check", "s1"),
            )


def test_warm_start_migration_adds_lifecycle_columns(isolated_handoff: dict) -> None:
    """A DB still at v9 must gain the new columns when reopened."""
    with _get_db_connection() as conn:
        conn.execute("ALTER TABLE review_findings DROP COLUMN resolved_on_branch_at_commit")
        conn.execute("ALTER TABLE review_findings DROP COLUMN resolved_on_branch_ref")
        conn.execute("ALTER TABLE review_findings DROP COLUMN resolved_on_branch_at_ts")
        conn.execute("ALTER TABLE review_findings DROP COLUMN integrated_at_commit")
        conn.execute("ALTER TABLE review_findings DROP COLUMN integrated_at_ref")
        conn.execute("ALTER TABLE review_findings DROP COLUMN integrated_at_ts")
        conn.execute("ALTER TABLE handoff_state DROP COLUMN last_observed_integration_sha")
        conn.execute("PRAGMA user_version = 9")
        conn.commit()

    raw = sqlite3.connect(isolated_handoff["db_path"])
    try:
        assert int(raw.execute("PRAGMA user_version").fetchone()[0]) == 9
        rf_cols = {row[1] for row in raw.execute("PRAGMA table_info(review_findings)").fetchall()}
        assert "resolved_on_branch_at_commit" not in rf_cols
        hs_cols = {row[1] for row in raw.execute("PRAGMA table_info(handoff_state)").fetchall()}
        assert "last_observed_integration_sha" not in hs_cols
    finally:
        raw.close()

    with _get_db_connection() as conn:
        cols = _table_columns(conn, "review_findings")
        for column in (
            "resolved_on_branch_at_commit",
            "resolved_on_branch_ref",
            "resolved_on_branch_at_ts",
            "integrated_at_commit",
            "integrated_at_ref",
            "integrated_at_ts",
        ):
            assert column in cols, f"warm-start v9->v10 must restore review_findings.{column}"
        assert "last_observed_integration_sha" in _table_columns(conn, "handoff_state")
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == HANDOFF_SCHEMA_VERSION


def test_warm_start_migration_preserves_existing_rows(isolated_handoff: dict) -> None:
    """The review_findings rebuild step must not drop data."""
    _parse(mcp_server.set_handoff_state(task_ref="mig-task", objective="mig task", status="in_progress"))
    _parse(
        mcp_server.record_review_finding(
            session="seed",
            finding_id="MIG-001",
            severity="medium",
            file_path="m.py",
            description="seed before migration",
            task_ref="mig-task",
        )
    )

    with _get_db_connection() as conn:
        conn.execute("ALTER TABLE review_findings DROP COLUMN resolved_on_branch_at_commit")
        conn.execute("ALTER TABLE review_findings DROP COLUMN resolved_on_branch_ref")
        conn.execute("ALTER TABLE review_findings DROP COLUMN resolved_on_branch_at_ts")
        conn.execute("ALTER TABLE review_findings DROP COLUMN integrated_at_commit")
        conn.execute("ALTER TABLE review_findings DROP COLUMN integrated_at_ref")
        conn.execute("ALTER TABLE review_findings DROP COLUMN integrated_at_ts")
        conn.execute("PRAGMA user_version = 9")
        conn.commit()

    with _get_db_connection() as conn:
        rows = conn.execute(
            "SELECT finding_id, description, status FROM review_findings WHERE task_ref = 'mig-task'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["finding_id"] == "MIG-001"
    assert rows[0]["description"] == "seed before migration"
    assert rows[0]["status"] == "open"


# ---------------------------------------------------------------------------
# implementation note: persistence anchor — verified_commit_sha lands on the row
# ---------------------------------------------------------------------------


def test_update_finding_persists_resolution_anchor_with_flag_off(
    isolated_handoff_flag_off: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``FLS-PLAN-01``: closing a finding from a descendant commit persists
    ``resolved_on_branch_at_commit`` / ``_ref`` / ``_at_ts`` on the row, even
    with ``finding_lifecycle_states_enabled=False``. implementation note wires the column
    writes irrespective of the flag; the status string stays ``fixed`` only
    when the flag is explicitly off (implementation note flipped the default to True)."""
    _mark_workspace_clean(monkeypatch)
    _parse(
        mcp_server.set_handoff_state(
            task_ref="anchor-task",
            objective="anchor",
            status="in_progress",
            target_branch="feature/anchor",
        )
    )
    _parse(
        mcp_server.record_review_finding(
            session="anchor",
            finding_id="ANCHOR-001",
            severity="medium",
            file_path="anchor.py",
            description="anchor persistence",
            task_ref="anchor-task",
            actor={"agent": "reviewer", "branch": "feature/anchor", "commit_sha": "a" * 40},
        )
    )

    from workstate_handoff_mcp import core as handoff_core

    actor_sha = "b" * 40
    monkeypatch.setattr(handoff_core, "_detect_git_write_context", lambda: ("feature/anchor", actor_sha))
    monkeypatch.setattr(
        "workstate_handoff_mcp.review_findings_updates._classify_commit_relation",
        lambda reference_sha, candidate_sha: (
            "descendant" if (reference_sha, candidate_sha) == ("a" * 40, actor_sha) else "same"
        ),
    )

    result = _parse(
        mcp_server.update_review_finding(
            finding_id="ANCHOR-001",
            task_ref="anchor-task",
            status="fixed",
            resolution_notes="Confirmed the descendant commit removes the reviewed defect.",
            verified_commit_sha=actor_sha,
            actor={"agent": "reviewer", "branch": "feature/anchor", "commit_sha": actor_sha},
        )
    )

    assert result["ok"] is True, result
    assert result["finding"]["status"] == "fixed"

    with _get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM review_findings WHERE finding_id = 'ANCHOR-001' AND task_ref = 'anchor-task'"
        ).fetchone()
    assert row is not None
    assert row["resolved_on_branch_at_commit"] == actor_sha, (
        "implementation note must persist the resolution anchor commit on close, even with the feature flag off"
    )
    assert row["resolved_on_branch_ref"] == "feature/anchor"
    assert row["resolved_on_branch_at_ts"] is not None
    assert row["integrated_at_commit"] is None
    assert row["integrated_at_ref"] is None
    assert row["integrated_at_ts"] is None


# ---------------------------------------------------------------------------
# implementation note: classifier + write paths + typed surface
# ---------------------------------------------------------------------------


def test_classifier_emits_resolved_on_branch_when_flag_on() -> None:
    """``WORKSTATE-REF-LIFECYCLE-03``: when the lifecycle flag is on, a descendant-commit
    close emits ``RESOLVED_ON_BRANCH`` instead of ``FIXED``, and the outcome
    carries ``resolution_anchor_commit`` pre-populated for the writer."""
    from workstate_handoff_mcp.review_finding_resolution import (
        ResolutionOutcomeKind,
        classify_resolution_outcome,
    )

    outcome = classify_resolution_outcome(
        finding_commit_sha="a" * 40,
        workspace_commit_sha="b" * 40,
        verified_commit_sha="b" * 40,
        commit_relation="descendant",
        has_uncommitted_changes=False,
        lifecycle_states_enabled=True,
    )

    assert outcome.kind is ResolutionOutcomeKind.RESOLVED_ON_BRANCH
    assert outcome.verified_commit_sha == "b" * 40
    assert outcome.resolution_anchor_commit == "b" * 40


def test_classifier_emits_fixed_when_flag_off() -> None:
    """Regression: with the flag off (default), the classifier still emits
    ``FIXED`` so legacy callers see no behavior change during the rollout
    window."""
    from workstate_handoff_mcp.review_finding_resolution import (
        ResolutionOutcomeKind,
        classify_resolution_outcome,
    )

    outcome = classify_resolution_outcome(
        finding_commit_sha="a" * 40,
        workspace_commit_sha="b" * 40,
        verified_commit_sha="b" * 40,
        commit_relation="descendant",
        has_uncommitted_changes=False,
    )

    assert outcome.kind is ResolutionOutcomeKind.FIXED
    assert outcome.resolution_anchor_commit is None


def test_classifier_unrelated_branch_still_blocked_when_flag_on() -> None:
    """``WORKSTATE-REF-LIFECYCLE-08``: the lifecycle flag must not loosen the
    descendant guard. A diverged relation still returns
    ``BLOCKED_BY_CONTEXT`` regardless of the flag value."""
    from workstate_handoff_mcp.review_finding_resolution import (
        ResolutionOutcomeKind,
        classify_resolution_outcome,
    )

    outcome = classify_resolution_outcome(
        finding_commit_sha="a" * 40,
        workspace_commit_sha="c" * 40,
        verified_commit_sha=None,
        commit_relation="diverged",
        has_uncommitted_changes=False,
        lifecycle_states_enabled=True,
    )

    assert outcome.kind is ResolutionOutcomeKind.BLOCKED_BY_CONTEXT
    assert outcome.resolution_anchor_commit is None


def test_update_finding_writes_resolved_on_branch_when_flag_on(
    isolated_handoff_flag_on: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end implementation note path: with the lifecycle flag on, closing a finding
    from a descendant commit writes ``status='resolved_on_branch'`` to the row
    (not the legacy ``'fixed'``). The resolution-anchor columns from implementation note
    must still be populated."""
    _mark_workspace_clean(monkeypatch)
    _parse(
        mcp_server.set_handoff_state(
            task_ref="flag-on-task",
            objective="flag on close",
            status="in_progress",
            target_branch="feature/flag-on",
        )
    )
    _parse(
        mcp_server.record_review_finding(
            session="flag-on",
            finding_id="FLAGON-001",
            severity="medium",
            file_path="flag_on.py",
            description="flag-on close transition",
            task_ref="flag-on-task",
            actor={"agent": "reviewer", "branch": "feature/flag-on", "commit_sha": "a" * 40},
        )
    )

    from workstate_handoff_mcp import core as handoff_core

    actor_sha = "b" * 40
    monkeypatch.setattr(handoff_core, "_detect_git_write_context", lambda: ("feature/flag-on", actor_sha))
    monkeypatch.setattr(
        "workstate_handoff_mcp.review_findings_updates._classify_commit_relation",
        lambda reference_sha, candidate_sha: (
            "descendant" if (reference_sha, candidate_sha) == ("a" * 40, actor_sha) else "same"
        ),
    )

    result = _parse(
        mcp_server.update_review_finding(
            finding_id="FLAGON-001",
            task_ref="flag-on-task",
            status="fixed",
            resolution_notes="Confirmed the descendant commit removes the reviewed defect.",
            verified_commit_sha=actor_sha,
            actor={"agent": "reviewer", "branch": "feature/flag-on", "commit_sha": actor_sha},
        )
    )

    assert result["ok"] is True, result
    assert result["finding"]["status"] == "resolved_on_branch", (
        "implementation note: when the lifecycle flag is on, fixed-close must persist 'resolved_on_branch'"
    )
    assert result["commit_guard"]["resolution_anchor_commit"] == actor_sha, (
        "implementation note: commit-guard envelope must surface the resolution anchor commit"
    )

    with _get_db_connection() as conn:
        row = conn.execute(
            "SELECT status, resolved_on_branch_at_commit, resolved_on_branch_ref"
            " FROM review_findings WHERE finding_id = 'FLAGON-001' AND task_ref = 'flag-on-task'"
        ).fetchone()
    assert row is not None
    assert row["status"] == "resolved_on_branch"
    assert row["resolved_on_branch_at_commit"] == actor_sha
    assert row["resolved_on_branch_ref"] == "feature/flag-on"


def test_direct_integrated_write_rejected_with_documented_error(
    isolated_handoff: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``WORKSTATE-REF-LIFECYCLE-09``: ``status='integrated'`` is integrate-managed.
    A direct ``update_review_finding(status='integrated')`` must be rejected
    with the documented error so operators are pushed toward the
    ``operation=integrate`` entry point."""
    _mark_workspace_clean(monkeypatch)
    _parse(
        mcp_server.set_handoff_state(
            task_ref="reject-task",
            objective="reject integrated",
            status="in_progress",
            target_branch="feature/reject",
        )
    )
    _parse(
        mcp_server.record_review_finding(
            session="reject",
            finding_id="REJECT-001",
            severity="medium",
            file_path="reject.py",
            description="reject direct integrated",
            task_ref="reject-task",
            actor={"agent": "reviewer", "branch": "feature/reject", "commit_sha": "a" * 40},
        )
    )

    result = _parse(
        mcp_server.update_review_finding(
            finding_id="REJECT-001",
            task_ref="reject-task",
            status="integrated",
            resolution_notes="should be rejected",
            actor={"agent": "reviewer", "branch": "feature/reject", "commit_sha": "a" * 40},
        )
    )

    assert result["ok"] is False, result
    error = str(result.get("error", ""))
    assert "integrate-managed" in error or "operation=integrate" in error, (
        f"implementation note: direct integrated write must point operators at the integrate operation; got: {error!r}"
    )


# ---------------------------------------------------------------------------
# implementation note: integrate operation + opportunistic trigger + reconcile contract
# ---------------------------------------------------------------------------


def _seed_resolved_on_branch_finding(
    *,
    task_ref: str,
    finding_id: str,
    anchor_commit: str,
    anchor_ref: str = "feature/integrate",
) -> None:
    """Insert a row already at status='resolved_on_branch' with the anchor
    commit populated. Used by implementation note tests to skip the close path and focus on
    the integrate promotion."""
    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO review_findings (
                finding_id, task_ref, severity, status, file_path, description, session,
                resolved_on_branch_at_commit, resolved_on_branch_ref, resolved_on_branch_at_ts
            ) VALUES (?, ?, 'medium', 'resolved_on_branch', 'i.py', 'integrate seed', 's-int',
                      ?, ?, datetime('now'))
            """,
            (finding_id, task_ref, anchor_commit, anchor_ref),
        )


def test_integrate_promotes_reachable_rows(isolated_handoff: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``resolved_on_branch`` row whose anchor commit is reachable from the
    integration ref is promoted to ``status='integrated'`` and the three
    ``integrated_at_*`` columns are populated."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="int-task",
            objective="integrate promote",
            status="in_progress",
            target_branch="feature/integrate",
        )
    )
    anchor = "c" * 40
    _seed_resolved_on_branch_finding(task_ref="int-task", finding_id="INT-001", anchor_commit=anchor)

    integration_head = "d" * 40

    from workstate_handoff_mcp import review_findings_updates as rfu

    monkeypatch.setattr(rfu, "_resolve_integration_ref_head_sha", lambda ref: integration_head)
    monkeypatch.setattr(rfu, "_is_ancestor_of_ref", lambda candidate, ref: True)

    result = _parse(mcp_server.integrate_review_findings(task_ref="int-task", integration_ref="main"))

    assert result["ok"] is True, result
    promoted_ids = [item["finding_id"] for item in result.get("promoted", [])]
    assert "INT-001" in promoted_ids

    with _get_db_connection() as conn:
        row = conn.execute(
            "SELECT status, integrated_at_commit, integrated_at_ref, integrated_at_ts"
            " FROM review_findings WHERE finding_id = 'INT-001' AND task_ref = 'int-task'"
        ).fetchone()
    assert row is not None
    assert row["status"] == "integrated"
    assert row["integrated_at_commit"] == integration_head
    assert row["integrated_at_ref"] == "main"
    assert row["integrated_at_ts"] is not None

    with _get_db_connection() as conn:
        decisions = conn.execute("SELECT decision FROM decisions WHERE task_ref = 'int-task'").fetchall()
    decision_ids = {str(d["decision"]) for d in decisions}
    assert any("INT-001" in did or "integrate" in did.lower() for did in decision_ids), (
        f"integrate_review_findings must record a decision per promotion; got: {decision_ids!r}"
    )


def test_integrate_skips_unreachable_rows(isolated_handoff: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``resolved_on_branch`` row whose anchor commit is NOT reachable from
    the integration ref stays at ``resolved_on_branch``; ``integrated_at_*``
    must remain null."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="skip-task",
            objective="integrate skip",
            status="in_progress",
            target_branch="feature/skip",
        )
    )
    anchor = "e" * 40
    _seed_resolved_on_branch_finding(task_ref="skip-task", finding_id="SKIP-001", anchor_commit=anchor)

    from workstate_handoff_mcp import review_findings_updates as rfu

    monkeypatch.setattr(rfu, "_resolve_integration_ref_head_sha", lambda ref: "f" * 40)
    monkeypatch.setattr(rfu, "_is_ancestor_of_ref", lambda candidate, ref: False)

    result = _parse(mcp_server.integrate_review_findings(task_ref="skip-task", integration_ref="main"))

    assert result["ok"] is True, result
    promoted_ids = [item["finding_id"] for item in result.get("promoted", [])]
    assert "SKIP-001" not in promoted_ids
    skipped_ids = [item["finding_id"] for item in result.get("skipped_unreachable", [])]
    assert "SKIP-001" in skipped_ids

    with _get_db_connection() as conn:
        row = conn.execute(
            "SELECT status, integrated_at_commit FROM review_findings"
            " WHERE finding_id = 'SKIP-001' AND task_ref = 'skip-task'"
        ).fetchone()
    assert row is not None
    assert row["status"] == "resolved_on_branch"
    assert row["integrated_at_commit"] is None


def test_opportunistic_trigger_debounced_by_last_observed_integration_sha(
    isolated_handoff: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opportunistic trigger on the host write paths only runs the
    integrate scan when the current integration-ref HEAD SHA differs from
    ``handoff_state.last_observed_integration_sha``. After a run lands, the
    sha is recorded and subsequent host writes at the same HEAD must not
    invoke integrate again. Advancing the HEAD must rearm the trigger."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="deb-task",
            objective="debounce",
            status="in_progress",
            target_branch="feature/debounce",
        )
    )

    from workstate_handoff_mcp import review_findings_updates as rfu

    calls: list[tuple[str | None, str]] = []
    original_integrate = rfu.integrate_review_findings

    def _spy(*, task_ref=None, integration_ref="main", actor=None):
        calls.append((task_ref, integration_ref))
        return original_integrate(task_ref=task_ref, integration_ref=integration_ref, actor=actor)

    monkeypatch.setattr(rfu, "integrate_review_findings", _spy)
    monkeypatch.setattr(rfu, "_resolve_integration_ref_head_sha", lambda ref: "1" * 40)
    monkeypatch.setattr(rfu, "_is_ancestor_of_ref", lambda candidate, ref: True)

    def _current_revision() -> int:
        with _get_db_connection() as conn:
            row = conn.execute("SELECT revision FROM handoff_state WHERE task_ref = 'deb-task'").fetchone()
        assert row is not None
        return int(row["revision"])

    first = _parse(
        mcp_server.set_handoff_state(
            task_ref="deb-task",
            focus="trigger first",
            status="in_progress",
            expected_revision=_current_revision(),
        )
    )
    assert first["ok"] is True, first
    assert len(calls) == 1, f"first host write should trigger integrate once; got {len(calls)} calls"

    with _get_db_connection() as conn:
        row = conn.execute(
            "SELECT last_observed_integration_sha FROM handoff_state WHERE task_ref = 'deb-task'"
        ).fetchone()
    assert row is not None
    assert row["last_observed_integration_sha"] == "1" * 40, (
        "opportunistic trigger must persist last_observed_integration_sha"
    )

    second = _parse(
        mcp_server.set_handoff_state(
            task_ref="deb-task",
            focus="trigger second same head",
            status="in_progress",
            expected_revision=_current_revision(),
        )
    )
    assert second["ok"] is True, second
    assert len(calls) == 1, (
        f"second host write at the same integration HEAD must not re-trigger integrate; got {len(calls)} calls"
    )

    monkeypatch.setattr(rfu, "_resolve_integration_ref_head_sha", lambda ref: "2" * 40)
    third = _parse(
        mcp_server.set_handoff_state(
            task_ref="deb-task",
            focus="trigger third new head",
            status="in_progress",
            expected_revision=_current_revision(),
        )
    )
    assert third["ok"] is True, third
    assert len(calls) == 2, (
        f"host write after the integration HEAD advances must re-trigger integrate; got {len(calls)} calls"
    )


def test_integrate_error_does_not_block_host_write(isolated_handoff: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """The opportunistic trigger must never propagate an integrate failure
    back to the host write path. If the git lookup or the integrate function
    raises, the host write still succeeds (logged + skipped)."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="err-task",
            objective="integrate error",
            status="in_progress",
            target_branch="feature/err",
        )
    )

    from workstate_handoff_mcp import review_findings_updates as rfu

    def _boom(*_args, **_kwargs):
        raise RuntimeError("integrate exploded")

    monkeypatch.setattr(rfu, "_resolve_integration_ref_head_sha", _boom)

    with _get_db_connection() as conn:
        row = conn.execute("SELECT revision FROM handoff_state WHERE task_ref = 'err-task'").fetchone()
    starting_revision = int(row["revision"])

    result = _parse(
        mcp_server.set_handoff_state(
            task_ref="err-task",
            focus="should still succeed",
            status="in_progress",
            expected_revision=starting_revision,
        )
    )
    assert result["ok"] is True, f"opportunistic integrate failure must not block the host write; got {result!r}"


def test_integrate_cap_applies_at_n_200(isolated_handoff: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """``integrate_review_findings`` caps each pass at N=200 promotions per
    task. Seeding 250 reachable resolved_on_branch rows must yield exactly 200
    integrated rows after one call, with ``cap_applied`` reported as True."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="cap-task",
            objective="integrate cap",
            status="in_progress",
            target_branch="feature/cap",
        )
    )

    with _get_db_connection() as conn:
        for idx in range(250):
            conn.execute(
                """
                INSERT INTO review_findings (
                    finding_id, task_ref, severity, status, file_path, description, session,
                    resolved_on_branch_at_commit, resolved_on_branch_ref, resolved_on_branch_at_ts
                ) VALUES (?, 'cap-task', 'medium', 'resolved_on_branch', 'c.py', 'cap seed',
                          's-cap', ?, 'feature/cap', datetime('now'))
                """,
                (f"CAP-{idx:03d}", f"{idx:040d}"),
            )

    from workstate_handoff_mcp import review_findings_updates as rfu

    monkeypatch.setattr(rfu, "_resolve_integration_ref_head_sha", lambda ref: "a" * 40)
    monkeypatch.setattr(rfu, "_is_ancestor_of_ref", lambda candidate, ref: True)

    result = _parse(mcp_server.integrate_review_findings(task_ref="cap-task", integration_ref="main"))

    assert result["ok"] is True, result
    promoted_count = len(result.get("promoted", []))
    assert promoted_count == 200, f"integrate must cap at 200 per pass; promoted {promoted_count}"
    assert result.get("cap_applied") is True, "cap_applied must be reported True when the limit fires"

    with _get_db_connection() as conn:
        integrated_count = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM review_findings WHERE task_ref = 'cap-task' AND status = 'integrated'"
            ).fetchone()["n"]
        )
        remaining_count = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM review_findings WHERE task_ref = 'cap-task' AND status = 'resolved_on_branch'"
            ).fetchone()["n"]
        )
    assert integrated_count == 200
    assert remaining_count == 50


def test_orchestrator_reconcile_review_findings_contract_unchanged(
    isolated_handoff: dict,
) -> None:
    """WORKSTATE68-PLAN-01 contract pin. The WORKSTATE-REF-41 orchestrator surface
    ``reconcile_review_findings(task_ref, apply)`` must remain unchanged in
    signature and return shape after WORKSTATE-REF-68 lands its parallel
    ``integrate_review_findings`` entry point. The two operations are
    distinct: ``reconcile`` is integrity/dedup; ``integrate`` is the
    lifecycle promotion."""
    import inspect

    from workstate_handoff_mcp.review_findings import reconcile_review_findings

    signature = inspect.signature(reconcile_review_findings)
    params = signature.parameters
    assert list(params.keys()) == ["task_ref", "apply"], (
        f"reconcile_review_findings signature must remain (task_ref, apply); got {list(params.keys())!r}"
    )
    assert params["task_ref"].default is None
    assert params["apply"].default is False

    _parse(
        mcp_server.set_handoff_state(
            task_ref="contract-task",
            objective="contract pin",
            status="in_progress",
            target_branch="feature/contract",
        )
    )

    raw = reconcile_review_findings(task_ref="contract-task", apply=False)
    result = _parse(raw)
    for key in ("ok", "task_ref", "healthy", "checks"):
        assert key in result, (
            f"reconcile_review_findings return shape must include {key!r}; got {sorted(result.keys())!r}"
        )
    assert result["ok"] is True
    assert result["task_ref"] == "contract-task"


# ---------------------------------------------------------------------------
# implementation note: receipt sentence templates for resolved findings
# ---------------------------------------------------------------------------


def test_receipt_renders_two_state_sentence_templates(isolated_handoff: dict) -> None:
    """WORKSTATE-REF-68 implementation note: the current-task receipt emits the two-anchor
    sentence templates for resolved-on-branch and integrated rows, and retains
    the legacy ``fixed`` UX text only for rows that did not migrate to the new
    columns (e.g. legacy fixtures pre-WORKSTATE-REF-68 schema migration)."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="recv-task",
            objective="receipt render",
            status="in_progress",
            target_branch="feature/recv",
        )
    )

    resolved_sha = "abcdef0123456789abcdef0123456789abcdef01"
    integrated_sha = "1234567abcdef0123456789abcdef0123456789ab"
    legacy_sha = "deadbeefcafef00d0000000000000000000000aa"

    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO review_findings (
                finding_id, task_ref, severity, status, file_path, description, session,
                branch, commit_sha,
                resolved_on_branch_at_commit, resolved_on_branch_ref, resolved_on_branch_at_ts
            ) VALUES ('RV-A', 'recv-task', 'medium', 'resolved_on_branch', 'a.py', 'on branch only',
                      's-recv', 'feature/recv', ?,
                      ?, 'feature/recv', datetime('now'))
            """,
            (resolved_sha, resolved_sha),
        )
        conn.execute(
            """
            INSERT INTO review_findings (
                finding_id, task_ref, severity, status, file_path, description, session,
                branch, commit_sha,
                integrated_at_commit, integrated_at_ref, integrated_at_ts
            ) VALUES ('RV-B', 'recv-task', 'medium', 'integrated', 'b.py', 'on main now',
                      's-recv', 'main', ?,
                      ?, 'main', datetime('now'))
            """,
            (integrated_sha, integrated_sha),
        )
        conn.execute(
            """
            INSERT INTO review_findings (
                finding_id, task_ref, severity, status, file_path, description, session,
                branch, commit_sha
            ) VALUES ('RV-C', 'recv-task', 'medium', 'fixed', 'c.py', 'legacy fixed row',
                      's-recv', 'feature/legacy', ?)
            """,
            (legacy_sha,),
        )

    from workstate_handoff_mcp.current_task_rendering import (
        _build_current_task_state_from_snapshot,
        _collect_task_snapshot,
        _render_current_task_md,
    )

    with _get_db_connection() as conn:
        snapshot = _collect_task_snapshot(conn, "recv-task")
    state = _build_current_task_state_from_snapshot(snapshot)
    md = _render_current_task_md(state)

    assert "## Resolved Findings" in md, md
    # Row A: resolved-on-branch sentence template, sha7 truncated.
    assert "RV-A" in md
    assert f"fixed on feature/recv@{resolved_sha[:7]}, pending integration to main" in md, md
    # Row B: integrated sentence template.
    assert "RV-B" in md
    assert f"integrated to main@{integrated_sha[:7]}" in md, md
    # Row C: legacy `fixed` UX text retained verbatim.
    assert "RV-C" in md
    assert "legacy fixed" in md.lower() or "fixed on" in md, md


# ---------------------------------------------------------------------------
# implementation note: backfill script for legacy `status='fixed'` rows
# ---------------------------------------------------------------------------


def test_backfill_finding_lifecycle_states_populates_anchor_columns(
    isolated_handoff: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKSTATE-REF-68 implementation note: ``backfill_finding_lifecycle_states`` walks legacy
    ``status='fixed'`` rows and derives a close-commit anchor per the documented
    priority: (a) 40-char SHA inside ``verification_evidence`` else
    (b) ``commit_sha`` from the most recent decision row referencing the
    finding id. Reachable anchors populate ``integrated_at_*``; unreachable or
    underivable anchors populate ``resolved_on_branch_at_*`` (with
    ``resolved_on_branch_at_commit`` left null when nothing could be derived).
    Every walked row must produce exactly one migration decision row.

    The legacy rows stay at ``status='fixed'`` during backfill — the implementation note
    flag flip is what changes the wire-level status string."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="bf-task",
            objective="backfill seed",
            status="in_progress",
            target_branch="feature/bf",
        )
    )

    reachable_sha = "1" * 40
    unreachable_sha = "2" * 40
    decision_sha = "3" * 40

    with _get_db_connection() as conn:
        # Row 1: evidence carries a reachable SHA -> integrated_at_*.
        conn.execute(
            """
            INSERT INTO review_findings (
                finding_id, task_ref, severity, status, file_path, description, session,
                verification_evidence
            ) VALUES ('BF-A', 'bf-task', 'medium', 'fixed', 'a.py', 'reachable evidence', 's-bf',
                      ?)
            """,
            (f"closed in {reachable_sha}",),
        )
        # Row 2: evidence carries an unreachable SHA -> resolved_on_branch_at_*.
        conn.execute(
            """
            INSERT INTO review_findings (
                finding_id, task_ref, severity, status, file_path, description, session,
                verification_evidence
            ) VALUES ('BF-B', 'bf-task', 'medium', 'fixed', 'b.py', 'unreachable evidence', 's-bf',
                      ?)
            """,
            (f"closed in {unreachable_sha}",),
        )
        # Row 3: no evidence SHA; matching decision row supplies commit_sha
        # (reachable) -> integrated_at_*.
        conn.execute(
            """
            INSERT INTO review_findings (
                finding_id, task_ref, severity, status, file_path, description, session
            ) VALUES ('BF-C', 'bf-task', 'medium', 'fixed', 'c.py', 'decision fallback', 's-bf')
            """
        )
        conn.execute(
            """
            INSERT INTO decisions (task_ref, session, decision, commit_sha)
            VALUES ('bf-task', 's-bf', 'fixed_BF-C_via_close', ?)
            """,
            (decision_sha,),
        )
        # Row 4: no evidence, no matching decision -> resolved_on_branch_at_commit=null.
        conn.execute(
            """
            INSERT INTO review_findings (
                finding_id, task_ref, severity, status, file_path, description, session
            ) VALUES ('BF-D', 'bf-task', 'medium', 'fixed', 'd.py', 'no derivation', 's-bf')
            """
        )

    reachable_set = {reachable_sha, decision_sha}

    from workstate_handoff_mcp.scripts import backfill_finding_lifecycle_states as bf

    monkeypatch.setattr(bf, "_resolve_integration_ref_head_sha", lambda ref: "9" * 40)
    monkeypatch.setattr(bf, "_is_ancestor_of_ref", lambda candidate, ref: candidate in reachable_set)

    report = bf.backfill_finding_lifecycle_states(integration_ref="main")

    assert report["ok"] is True, report
    assert report["walked"] == 4
    assert report["migration_decisions"] == 4

    with _get_db_connection() as conn:
        rows = {
            row["finding_id"]: row
            for row in conn.execute(
                """
                SELECT finding_id, status, resolved_on_branch_at_commit, resolved_on_branch_at_ts,
                       integrated_at_commit, integrated_at_ref, integrated_at_ts
                FROM review_findings WHERE task_ref = 'bf-task'
                """
            ).fetchall()
        }

    # All four legacy rows stay at status='fixed' (wire-compat; flag flip
    # changes the string in implementation note).
    for finding_id in ("BF-A", "BF-B", "BF-C", "BF-D"):
        assert rows[finding_id]["status"] == "fixed", rows[finding_id]

    # Row 1: reachable evidence anchor -> integrated columns populated.
    assert rows["BF-A"]["integrated_at_commit"] == reachable_sha
    assert rows["BF-A"]["integrated_at_ref"] == "main"
    assert rows["BF-A"]["integrated_at_ts"] is not None
    # Row 2: unreachable evidence anchor -> resolved_on_branch columns populated.
    assert rows["BF-B"]["resolved_on_branch_at_commit"] == unreachable_sha
    assert rows["BF-B"]["resolved_on_branch_at_ts"] is not None
    assert rows["BF-B"]["integrated_at_commit"] is None
    # Row 3: decision-fallback SHA reachable -> integrated columns populated.
    assert rows["BF-C"]["integrated_at_commit"] == decision_sha
    assert rows["BF-C"]["integrated_at_ref"] == "main"
    # Row 4: no derivable anchor -> resolved_on_branch_at_commit stays null;
    # _at_ts is still populated so the migration is traceable.
    assert rows["BF-D"]["resolved_on_branch_at_commit"] is None
    assert rows["BF-D"]["resolved_on_branch_at_ts"] is not None
    assert rows["BF-D"]["integrated_at_commit"] is None

    # Exactly one migration decision row per walked finding.
    with _get_db_connection() as conn:
        migration_decisions = [
            str(d["decision"])
            for d in conn.execute(
                "SELECT decision FROM decisions WHERE task_ref = 'bf-task'"
                " AND decision LIKE 'backfill_finding_lifecycle_%'"
            ).fetchall()
        ]
    assert len(migration_decisions) == 4, migration_decisions
    for finding_id in ("BF-A", "BF-B", "BF-C", "BF-D"):
        assert any(finding_id in did for did in migration_decisions), (
            f"missing migration decision for {finding_id}; got {migration_decisions!r}"
        )


# ---------------------------------------------------------------------------
# implementation note: dashboard breakdown for resolved findings
# ---------------------------------------------------------------------------


def test_dashboard_renders_resolved_findings_breakdown(isolated_handoff: dict) -> None:
    """WORKSTATE-REF-68 implementation note: ``generate_dashboard_md`` projects the two-state
    finding lifecycle into a ``RESOLVED FINDINGS`` section, grouping rows by
    ``task_ref`` and reporting per-task ``resolved_on_branch`` vs
    ``integrated`` counts plus a one-line receipt for each row that mirrors
    the current-task template (`fixed on <branch>@<sha7>, pending integration
    to main` / `integrated to <ref>@<sha7>`). Legacy ``fixed`` rows that have
    not been backfilled are also surfaced under the same section with the
    legacy fallback line."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="dash-task",
            objective="dashboard render",
            status="in_progress",
            target_branch="feature/dash",
        )
    )

    resolved_sha = "aaaaaaa0123456789abcdef0123456789abcdef0"
    integrated_sha = "bbbbbbb123456789abcdef0123456789abcdef01"
    legacy_sha = "ccccccc23456789abcdef0123456789abcdef012"

    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO review_findings (
                finding_id, task_ref, severity, status, file_path, description, session,
                branch, commit_sha,
                resolved_on_branch_at_commit, resolved_on_branch_ref, resolved_on_branch_at_ts
            ) VALUES ('DR-A', 'dash-task', 'medium', 'resolved_on_branch', 'a.py', 'on branch only',
                      's-dash', 'feature/dash', ?,
                      ?, 'feature/dash', datetime('now'))
            """,
            (resolved_sha, resolved_sha),
        )
        conn.execute(
            """
            INSERT INTO review_findings (
                finding_id, task_ref, severity, status, file_path, description, session,
                branch, commit_sha,
                integrated_at_commit, integrated_at_ref, integrated_at_ts
            ) VALUES ('DR-B', 'dash-task', 'medium', 'integrated', 'b.py', 'on main now',
                      's-dash', 'main', ?,
                      ?, 'main', datetime('now'))
            """,
            (integrated_sha, integrated_sha),
        )
        conn.execute(
            """
            INSERT INTO review_findings (
                finding_id, task_ref, severity, status, file_path, description, session,
                branch, commit_sha
            ) VALUES ('DR-C', 'dash-task', 'medium', 'fixed', 'c.py', 'legacy fixed row',
                      's-dash', 'feature/legacy', ?)
            """,
            (legacy_sha,),
        )

    from workstate_handoff_mcp.dashboard_rendering import generate_dashboard_md

    result = generate_dashboard_md(write_file=False)
    md = result["markdown"]

    assert "RESOLVED FINDINGS" in md, md
    assert "[dash-task]" in md, md
    # Per-task counts breakdown for the two new states + legacy rows.
    assert "resolved_on_branch=1" in md, md
    assert "integrated=1" in md, md
    # Per-finding receipts mirror the current-task sentence templates.
    assert "DR-A" in md
    assert f"fixed on feature/dash@{resolved_sha[:7]}, pending integration to main" in md, md
    assert "DR-B" in md
    assert f"integrated to main@{integrated_sha[:7]}" in md, md
    assert "DR-C" in md
    assert f"feature/legacy@{legacy_sha[:7]}" in md, md


# ---------------------------------------------------------------------------
# implementation note: flip finding_lifecycle_states_enabled default to True
# ---------------------------------------------------------------------------


def test_lifecycle_flag_default_is_true(tmp_path: Path) -> None:
    """WORKSTATE-REF-68 implementation note: the lifecycle flag defaults to ``True`` so consumers
    that build a ``RuntimeConfig`` without explicitly opting in receive the
    two-state behavior. Explicit ``False`` (and the legacy
    ``WORKSTATE_HANDOFF_FINDING_LIFECYCLE_STATES=0`` env override) must still work
    as escape hatches."""
    workspace_root = tmp_path / "ws-default"
    workspace_root.mkdir()
    runtime = RuntimeConfig.for_workspace(workspace_root)
    assert runtime.finding_lifecycle_states_enabled is True

    runtime_off = RuntimeConfig.for_workspace(
        workspace_root,
        finding_lifecycle_states_enabled=False,
    )
    assert runtime_off.finding_lifecycle_states_enabled is False


def test_close_finding_uses_resolved_on_branch_without_explicit_flag(
    isolated_handoff: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKSTATE-REF-68 implementation note: with the lifecycle flag defaulting to True, a normal
    ``fixed`` close on the descendant-commit path persists
    ``status='resolved_on_branch'`` without the test having to opt in via
    ``isolated_handoff_flag_on``. WORKSTATE-REF-02's reproduction path now closes via
    the CLI alone — no env-var dance, no fixture override."""
    _mark_workspace_clean(monkeypatch)
    _parse(
        mcp_server.set_handoff_state(
            task_ref="default-task",
            objective="default close",
            status="in_progress",
            target_branch="feature/default",
        )
    )
    _parse(
        mcp_server.record_review_finding(
            session="default",
            finding_id="DEF-001",
            severity="medium",
            file_path="default.py",
            description="default close transition",
            task_ref="default-task",
            actor={"agent": "reviewer", "branch": "feature/default", "commit_sha": "a" * 40},
        )
    )

    from workstate_handoff_mcp import core as handoff_core

    actor_sha = "b" * 40
    monkeypatch.setattr(handoff_core, "_detect_git_write_context", lambda: ("feature/default", actor_sha))
    monkeypatch.setattr(
        "workstate_handoff_mcp.review_findings_updates._classify_commit_relation",
        lambda reference_sha, candidate_sha: (
            "descendant" if (reference_sha, candidate_sha) == ("a" * 40, actor_sha) else "same"
        ),
    )

    result = _parse(
        mcp_server.update_review_finding(
            finding_id="DEF-001",
            task_ref="default-task",
            status="fixed",
            resolution_notes="Confirmed the descendant commit removes the reviewed defect.",
            verified_commit_sha=actor_sha,
            actor={"agent": "reviewer", "branch": "feature/default", "commit_sha": actor_sha},
        )
    )

    assert result["ok"] is True, result
    assert result["finding"]["status"] == "resolved_on_branch", (
        "implementation note: the flag default flip means the unmodified isolated_handoff "
        "fixture must persist 'resolved_on_branch' after a clean descendant close."
    )


def test_list_status_fixed_returns_wire_compat_union(isolated_handoff: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """WORKSTATE-REF-68 implementation note (BR-001 fix): per the Status / Filter Compatibility
    Matrix in the task plan (row ``fixed``), ``list_review_findings(status='fixed')``
    must return the union of rows with ``status IN ('fixed','resolved_on_branch',
    'integrated')`` during the rollout window. This preserves wire-compat for
    legacy consumers filtering on the historical close state."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="union-task",
            objective="list fixed union",
            status="in_progress",
            target_branch="feature/union",
        )
    )
    for slug, status_value in (
        ("FIXED-A", "fixed"),
        ("ROB-B", "resolved_on_branch"),
        ("INT-C", "integrated"),
    ):
        _parse(
            mcp_server.record_review_finding(
                session="union",
                finding_id=slug,
                severity="medium",
                file_path=f"{slug.lower()}.py",
                description=f"seed {status_value}",
                task_ref="union-task",
                actor={"agent": "reviewer", "branch": "feature/union", "commit_sha": "a" * 40},
            )
        )

    with _get_db_connection() as conn:
        for slug, status_value in (
            ("FIXED-A", "fixed"),
            ("ROB-B", "resolved_on_branch"),
            ("INT-C", "integrated"),
        ):
            conn.execute(
                "UPDATE review_findings SET status = ? WHERE task_ref = 'union-task' AND finding_id = ?",
                (status_value, slug),
            )
        conn.commit()

    result = _parse(mcp_server.list_review_findings(task_ref="union-task", status="fixed"))
    assert result["ok"] is True, result
    finding_ids = {row["finding_id"] for row in result["findings"]}
    assert finding_ids == {"FIXED-A", "ROB-B", "INT-C"}, (
        "implementation note (BR-001): status='fixed' filter must return the wire-compat union "
        f"of fixed/resolved_on_branch/integrated. Got: {finding_ids}"
    )


def test_explicit_resolved_on_branch_update_permitted_when_flag_on(
    isolated_handoff_flag_on: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKSTATE-REF-68 implementation note (BR-002 fix): per the task plan's Update Path × Flag
    matrix (line 135), ``update(status='resolved_on_branch')`` must be permitted
    when ``finding_lifecycle_states_enabled`` is True. The row is written with
    ``status='resolved_on_branch'`` directly and the resolution-anchor columns
    are populated identically to the ``status='fixed'`` close path."""
    _mark_workspace_clean(monkeypatch)
    _parse(
        mcp_server.set_handoff_state(
            task_ref="explicit-rob-task",
            objective="explicit resolved_on_branch",
            status="in_progress",
            target_branch="feature/explicit-rob",
        )
    )
    _parse(
        mcp_server.record_review_finding(
            session="explicit-rob",
            finding_id="EXP-ROB-001",
            severity="medium",
            file_path="explicit_rob.py",
            description="explicit resolved_on_branch update",
            task_ref="explicit-rob-task",
            actor={"agent": "reviewer", "branch": "feature/explicit-rob", "commit_sha": "a" * 40},
        )
    )

    from workstate_handoff_mcp import core as handoff_core

    actor_sha = "b" * 40
    monkeypatch.setattr(handoff_core, "_detect_git_write_context", lambda: ("feature/explicit-rob", actor_sha))
    monkeypatch.setattr(
        "workstate_handoff_mcp.review_findings_updates._classify_commit_relation",
        lambda reference_sha, candidate_sha: (
            "descendant" if (reference_sha, candidate_sha) == ("a" * 40, actor_sha) else "same"
        ),
    )

    result = _parse(
        mcp_server.update_review_finding(
            finding_id="EXP-ROB-001",
            task_ref="explicit-rob-task",
            status="resolved_on_branch",
            resolution_notes="Confirmed the descendant commit removes the reviewed defect.",
            verified_commit_sha=actor_sha,
            actor={"agent": "reviewer", "branch": "feature/explicit-rob", "commit_sha": actor_sha},
        )
    )

    assert result["ok"] is True, result
    assert result["finding"]["status"] == "resolved_on_branch", (
        "implementation note (BR-002): explicit resolved_on_branch update must persist that exact status."
    )

    with _get_db_connection() as conn:
        row = conn.execute(
            "SELECT status, resolved_on_branch_at_commit, resolved_on_branch_ref"
            " FROM review_findings WHERE finding_id = 'EXP-ROB-001' AND task_ref = 'explicit-rob-task'"
        ).fetchone()
    assert row is not None
    assert row["status"] == "resolved_on_branch"
    assert row["resolved_on_branch_at_commit"] == actor_sha
    assert row["resolved_on_branch_ref"] == "feature/explicit-rob"


def test_explicit_resolved_on_branch_update_rejected_when_flag_off(
    isolated_handoff_flag_off: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKSTATE-REF-68 implementation note (BR-002 fix): the BR-002 fix is gated on the lifecycle
    flag. When the flag is off, explicit ``status='resolved_on_branch'`` updates
    must still be rejected with the documented escape-hatch error so legacy
    deployments retain single-state semantics."""
    _mark_workspace_clean(monkeypatch)
    _parse(
        mcp_server.set_handoff_state(
            task_ref="reject-rob-task",
            objective="reject explicit resolved_on_branch",
            status="in_progress",
            target_branch="feature/reject-rob",
        )
    )
    _parse(
        mcp_server.record_review_finding(
            session="reject-rob",
            finding_id="REJ-ROB-001",
            severity="medium",
            file_path="reject_rob.py",
            description="reject explicit resolved_on_branch when flag off",
            task_ref="reject-rob-task",
            actor={"agent": "reviewer", "branch": "feature/reject-rob", "commit_sha": "a" * 40},
        )
    )

    result = _parse(
        mcp_server.update_review_finding(
            finding_id="REJ-ROB-001",
            task_ref="reject-rob-task",
            status="resolved_on_branch",
            resolution_notes="Should be rejected because flag is off.",
            actor={"agent": "reviewer", "branch": "feature/reject-rob", "commit_sha": "b" * 40},
        )
    )

    assert result["ok"] is False
    assert "resolved_on_branch" in result["error"]
    assert "finding_lifecycle_states_enabled" in result["error"]
