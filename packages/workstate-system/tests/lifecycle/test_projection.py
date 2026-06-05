"""implementation note contract tests for the projection adapter wrapper.

The wrapper mirrors a lifecycle-runner decision into the handoff DB
via ``mcp-workstate-handoff event record``. When the adapter is
unavailable the call must spool the payload to
``.task-state/pending-workflow-events.jsonl`` and report
``handoff_projection: "pending"``; the underlying git operation
never blocks on adapter failure. Receipt-status mapping:

* ``synced`` — adapter exited 0 (response may be parseable or fall
  back to the supplied decision id).
* ``pending`` — adapter exited non-zero or could not be invoked.
* ``error`` — caller passes a malformed payload.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "workstate_system" / "payload" / "scripts" / "workstate" / "lifecycle"
PENDING_REL = Path(".task-state") / "pending-workflow-events.jsonl"


@pytest.fixture
def projection():
    sys.path.insert(0, str(LIFECYCLE_PKG))
    try:
        # projection.py uses `from handlers import _common`, the same
        # absolute-import pattern as cli.py — sys.path[0] = lifecycle
        # dir lets `handlers` resolve as a sub-package.
        for mod in ("projection", "handlers", "handlers._common"):
            if mod in sys.modules:
                del sys.modules[mod]
        import projection as p  # type: ignore[import-not-found]

        return p
    finally:
        sys.path.remove(str(LIFECYCLE_PKG))


def _write_fake_cli(target: Path, body: str) -> None:
    target.write_text(body)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_cli(tmp_path: Path):
    target = tmp_path / "fake-mcp"

    def _set(body: str) -> Path:
        _write_fake_cli(target, body)
        os.environ["MCP_WORKSTATE_HANDOFF_BIN"] = str(target)
        return target

    yield _set
    os.environ.pop("MCP_WORKSTATE_HANDOFF_BIN", None)


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def test_project_decision_synced_with_returned_id(
    projection, fake_cli, repo_root: Path
) -> None:
    fake_cli(
        '#!/usr/bin/env bash\n'
        'echo \'{"ok":true,"data":{"decision_id":"server_id_777"}}\'\n'
        'exit 0\n'
    )
    status, returned = projection.project_decision(
        repo_root,
        decision_id="claude_workflow_ambiguity_resolved_x",
        rationale="picked branch task over MCP active",
        session="claude_ctx_2026",
    )
    assert status == "synced"
    assert returned == "server_id_777"
    assert not (repo_root / PENDING_REL).exists()


def test_project_decision_synced_falls_back_to_supplied_id(
    projection, fake_cli, repo_root: Path
) -> None:
    fake_cli('#!/usr/bin/env bash\necho not-json-but-zero-exit\nexit 0\n')
    status, returned = projection.project_decision(
        repo_root,
        decision_id="claude_my_decision_1",
        rationale="r",
        session="s",
    )
    assert status == "synced"
    assert returned == "claude_my_decision_1"


def test_project_decision_adapter_rejection_spools_spooled(
    projection, fake_cli, repo_root: Path
) -> None:
    """WORKSTATE-REF-52 implementation note: CLI ran but rejected (exit 1) -> ``spooled``."""
    fake_cli('#!/usr/bin/env bash\necho boom 1>&2\nexit 1\n')
    status, returned = projection.project_decision(
        repo_root,
        decision_id="claude_pending_x",
        rationale="r",
        session="s",
    )
    assert status == "spooled"
    assert returned is None
    spool = repo_root / PENDING_REL
    assert spool.exists()
    payload = json.loads(spool.read_text().splitlines()[-1])
    assert payload["kind"] == "decision"
    assert payload["decision_id"] == "claude_pending_x"
    assert payload["rationale"] == "r"
    assert payload["session"] == "s"


def test_project_decision_missing_cli_spools_pending(
    projection, repo_root: Path
) -> None:
    os.environ["MCP_WORKSTATE_HANDOFF_BIN"] = "/nonexistent/path/no-such-binary-xyz"
    try:
        status, returned = projection.project_decision(
            repo_root,
            decision_id="claude_missing_cli",
            rationale="r",
            session="s",
        )
    finally:
        os.environ.pop("MCP_WORKSTATE_HANDOFF_BIN", None)
    assert status == "pending"
    assert returned is None
    assert (repo_root / PENDING_REL).exists()


def test_pending_spool_appends_one_line_per_call(
    projection, fake_cli, repo_root: Path
) -> None:
    fake_cli('#!/usr/bin/env bash\nexit 1\n')
    for n in range(3):
        projection.project_decision(
            repo_root,
            decision_id=f"d{n}",
            rationale="r",
            session="s",
        )
    lines = (repo_root / PENDING_REL).read_text().splitlines()
    assert len(lines) == 3
    decision_ids = [json.loads(line)["decision_id"] for line in lines]
    assert decision_ids == ["d0", "d1", "d2"]


def test_project_decision_passes_workspace_root_argv(
    projection, fake_cli, tmp_path: Path
) -> None:
    """Adapter must thread --workspace-root so a linked-worktree call
    targets the canonical handoff DB. Regression for BR-WORKSTATE40-S2-01."""
    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=primary, check=True)
    subprocess.run(
        [
            "git", "-C", str(primary),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "--allow-empty", "-m", "init", "-q",
        ],
        check=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        [
            "git", "-C", str(primary),
            "worktree", "add", "-q", "-b", "feature/WORKSTATE-7-w", str(linked),
        ],
        check=True,
    )
    argv_log = tmp_path / "argv.log"
    fake_cli(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {argv_log}\necho ok\nexit 0\n'
    )
    status, _id = projection.project_decision(
        linked,
        decision_id="claude_ws_root_check",
        rationale="r",
        session="s",
    )
    assert status == "synced"
    logged = argv_log.read_text().splitlines()
    assert "--workspace-root" in logged, f"missing --workspace-root in argv: {logged!r}"
    ws_index = logged.index("--workspace-root")
    # Adapter must point at the primary repo, not the linked worktree.
    assert logged[ws_index + 1] == str(primary), (
        f"--workspace-root pointed at {logged[ws_index + 1]!r}, "
        f"expected primary {str(primary)!r}"
    )


def test_project_state_sync_passes_full_argv(
    projection, fake_cli, tmp_path: Path
) -> None:
    """BR-WORKSTATE40-S3-02: state-sync must shell out to ``handoff set``
    with --task-ref, --target-branch, --target-worktree-path,
    --task-plan-path, --status, plus --workspace-root threaded for
    canonical-DB targeting.
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=primary, check=True)
    subprocess.run(
        [
            "git", "-C", str(primary),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "--allow-empty", "-m", "init", "-q",
        ],
        check=True,
    )
    argv_log = tmp_path / "argv.log"
    fake_cli(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {argv_log}\necho ok\nexit 0\n'
    )
    status = projection.project_state_sync(
        primary,
        task_ref="WORKSTATE-REF-77",
        target_branch="feature/WORKSTATE-77",
        target_worktree_path="/tmp/wt-77",
        task_plan_path="docs/plans/0009.md",
    )
    assert status == "synced", status
    logged = argv_log.read_text().splitlines()
    assert "set" in logged, logged
    for flag, expected in (
        ("--task-ref", "WORKSTATE-REF-77"),
        ("--target-branch", "feature/WORKSTATE-77"),
        ("--target-worktree-path", "/tmp/wt-77"),
        ("--task-plan-path", "docs/plans/0009.md"),
        ("--status", "in_progress"),
        ("--branch", "feature/WORKSTATE-77"),
    ):
        assert flag in logged, f"missing {flag} in {logged!r}"
        assert logged[logged.index(flag) + 1] == expected, logged
    assert "--workspace-root" in logged
    ws_index = logged.index("--workspace-root")
    assert logged[ws_index + 1] == str(primary)


def test_project_state_sync_passes_worktree_actor_overrides(
    projection, fake_cli, tmp_path: Path
) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=primary, check=True)
    subprocess.run(
        [
            "git", "-C", str(primary),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "--allow-empty", "-m", "init", "-q",
        ],
        check=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        [
            "git", "-C", str(primary),
            "worktree", "add", "-q", "-b", "feature/WORKSTATE-82", str(linked),
        ],
        check=True,
    )
    linked_head = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    argv_log = tmp_path / "argv.log"
    fake_cli(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {argv_log}\necho ok\nexit 0\n'
    )

    status = projection.project_state_sync(
        primary,
        task_ref="WORKSTATE-REF-82",
        target_branch="feature/WORKSTATE-82",
        target_worktree_path=str(linked),
        task_plan_path="packages/example/docs/tasks/WORKSTATE-REF-82-task-plan.md",
    )

    assert status == "synced", status
    logged = argv_log.read_text().splitlines()
    assert logged[logged.index("--workspace-root") + 1] == str(primary)
    assert logged[logged.index("--branch") + 1] == "feature/WORKSTATE-82"
    assert logged[logged.index("--commit-sha") + 1] == linked_head


def test_project_state_sync_rejection_spools_spooled(
    projection, fake_cli, repo_root: Path
) -> None:
    """WORKSTATE-REF-52 implementation note: CLI ran but rejected (exit 1) -> ``spooled``."""
    fake_cli('#!/usr/bin/env bash\nexit 1\n')
    status = projection.project_state_sync(
        repo_root,
        task_ref="WORKSTATE-REF-88",
        target_branch="feature/WORKSTATE-88",
        target_worktree_path="/tmp/wt-88",
        task_plan_path=None,
    )
    assert status == "spooled"
    spool = repo_root / PENDING_REL
    assert spool.exists()
    payload = json.loads(spool.read_text().splitlines()[-1])
    assert payload["kind"] == "state_sync"
    assert payload["task_ref"] == "WORKSTATE-REF-88"
    assert payload["target_branch"] == "feature/WORKSTATE-88"
    assert payload["target_worktree_path"] == "/tmp/wt-88"
    assert payload.get("task_plan_path") in (None, "")


def test_project_state_sync_forwards_objective(
    projection, fake_cli, tmp_path: Path
) -> None:
    """WS-TASKSTART: ``set`` must carry ``--objective`` so the very first
    sync for a brand-new task_ref can INSERT the row. ``set_handoff_state``
    rejects an objective-less insert, so dropping the flag is the silent
    no-op behind the missing handoff_state row.
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=primary, check=True)
    subprocess.run(
        [
            "git", "-C", str(primary),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "--allow-empty", "-m", "init", "-q",
        ],
        check=True,
    )
    argv_log = tmp_path / "argv.log"
    fake_cli(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {argv_log}\necho ok\nexit 0\n'
    )
    status = projection.project_state_sync(
        primary,
        task_ref="WS-OBJ-01",
        target_branch="feature/ws-obj-01",
        target_worktree_path=str(primary),
        task_plan_path=None,
        objective="make the thing work",
    )
    assert status == "synced", status
    logged = argv_log.read_text().splitlines()
    assert "--objective" in logged, f"missing --objective in {logged!r}"
    assert logged[logged.index("--objective") + 1] == "make the thing work"


def test_project_state_sync_omits_objective_when_none(
    projection, fake_cli, tmp_path: Path
) -> None:
    """No ``--objective`` flag when the caller passes ``objective=None`` —
    an update to an existing row preserves its stored objective server-side.
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=primary, check=True)
    subprocess.run(
        [
            "git", "-C", str(primary),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "--allow-empty", "-m", "init", "-q",
        ],
        check=True,
    )
    argv_log = tmp_path / "argv.log"
    fake_cli(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {argv_log}\necho ok\nexit 0\n'
    )
    projection.project_state_sync(
        primary,
        task_ref="WS-OBJ-02",
        target_branch="feature/ws-obj-02",
        target_worktree_path=str(primary),
        task_plan_path=None,
    )
    logged = argv_log.read_text().splitlines()
    assert "--objective" not in logged


def test_project_state_sync_zero_exit_ok_false_is_spooled(
    projection, fake_cli, repo_root: Path
) -> None:
    """WS-TASKSTART core fix: the handoff CLI prints its envelope and
    *always exits 0*, so a real rejection (``{"ok": false}`` with no
    ``current_revision`` — e.g. a genuine insert failure) would be
    misclassified as ``synced`` and the row would silently never land.
    A zero-exit ``ok:false`` envelope must downgrade to ``spooled`` and
    spool the payload (audit trail + honest non-synced signal).
    """
    fake_cli(
        '#!/usr/bin/env bash\n'
        'echo \'{"ok":false,"data":{"error":"objective is required '
        'when creating a new handoff state."}}\'\n'
        'exit 0\n'
    )
    status = projection.project_state_sync(
        repo_root,
        task_ref="WS-OBJ-03",
        target_branch="feature/ws-obj-03",
        target_worktree_path="/tmp/wt-ws-obj-03",
        task_plan_path=None,
        objective="real objective",
    )
    assert status == "spooled", (
        f"zero-exit ok:false must not report 'synced', got {status!r}"
    )
    spool = repo_root / PENDING_REL
    assert spool.exists()
    payload = json.loads(spool.read_text().splitlines()[-1])
    assert payload["kind"] == "state_sync"
    assert payload["task_ref"] == "WS-OBJ-03"
    assert payload["objective"] == "real objective"


def test_project_state_sync_existing_row_revision_required_is_synced(
    projection, fake_cli, repo_root: Path
) -> None:
    """Regression for the review finding: a zero-exit ``ok:false`` whose
    ``data`` carries ``current_revision`` is the BENIGN "row already exists,
    update needs expected_revision" case (claim mode / re-running task-start).
    task-start's invariant — a live row exists for the task_ref — already
    holds, so this must report ``synced`` and spool NOTHING (the state_sync
    replay path cannot drain such an entry, so spooling it would accumulate
    undrainable noise forever).
    """
    fake_cli(
        '#!/usr/bin/env bash\n'
        'echo \'{"ok":false,"data":{"error":"expected_revision is required '
        'for updates.","current_revision":3}}\'\n'
        'exit 0\n'
    )
    status = projection.project_state_sync(
        repo_root,
        task_ref="WS-OBJ-05",
        target_branch="feature/ws-obj-05",
        target_worktree_path="/tmp/wt-ws-obj-05",
        task_plan_path=None,
        objective="real objective",
    )
    assert status == "synced", (
        f"benign row-exists rejection must report 'synced', got {status!r}"
    )
    assert not (repo_root / PENDING_REL).exists(), (
        "must not spool an undrainable state_sync entry for the row-exists case"
    )


def test_project_state_sync_zero_exit_ok_true_is_synced(
    projection, fake_cli, repo_root: Path
) -> None:
    """The ok-envelope guard must not regress the happy path: a zero-exit
    ``{"ok": true}`` envelope stays ``synced`` and spools nothing.
    """
    fake_cli(
        '#!/usr/bin/env bash\n'
        'echo \'{"ok":true,"data":{"inserted":true}}\'\n'
        'exit 0\n'
    )
    status = projection.project_state_sync(
        repo_root,
        task_ref="WS-OBJ-04",
        target_branch="feature/ws-obj-04",
        target_worktree_path="/tmp/wt-ws-obj-04",
        task_plan_path=None,
        objective="real objective",
    )
    assert status == "synced", status
    assert not (repo_root / PENDING_REL).exists()


def test_project_state_sync_omits_blank_task_plan_path(
    projection, fake_cli, tmp_path: Path
) -> None:
    """When no plan path is known, --task-plan-path must not be sent."""
    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=primary, check=True)
    subprocess.run(
        [
            "git", "-C", str(primary),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "--allow-empty", "-m", "init", "-q",
        ],
        check=True,
    )
    argv_log = tmp_path / "argv.log"
    fake_cli(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {argv_log}\nexit 0\n'
    )
    projection.project_state_sync(
        primary,
        task_ref="WORKSTATE-REF-90",
        target_branch="feature/WORKSTATE-90",
        target_worktree_path="/tmp/wt-90",
        task_plan_path=None,
    )
    logged = argv_log.read_text().splitlines()
    assert "--task-plan-path" not in logged


def test_project_test_result_passes_worktree_provenance(
    projection, fake_cli, tmp_path: Path
) -> None:
    """implementation note fix: ``project_test_result`` must record verified-test
    provenance under the *active worktree's* branch and commit, not the
    canonical workspace HEAD. Regression for finding
    ``codex_WORKSTATE40_slice4_projection_provenance_drift`` (id 151) — the
    handoff DB still lives at the primary worktree (so
    ``--workspace-root`` keeps targeting the canonical root) but
    ``--branch`` / ``--commit-sha`` actor overrides must reflect the
    feature worktree HEAD that produced the test result.
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=primary, check=True)
    subprocess.run(
        [
            "git", "-C", str(primary),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "--allow-empty", "-m", "init", "-q",
        ],
        check=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        [
            "git", "-C", str(primary),
            "worktree", "add", "-q", "-b", "feature/WORKSTATE-77", str(linked),
        ],
        check=True,
    )
    # Diverge linked-worktree HEAD from primary HEAD so a drift bug
    # cannot accidentally pass with matching shas.
    (linked / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(linked), "add", "f.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(linked),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "-m", "feature commit", "-q",
        ],
        check=True,
    )
    linked_head = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    primary_head = subprocess.run(
        ["git", "-C", str(primary), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert linked_head != primary_head

    argv_log = tmp_path / "argv.log"
    fake_cli(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {argv_log}\necho ok\nexit 0\n'
    )
    status = projection.project_test_result(
        linked,
        session="claude_slice_started_test",
        command="pytest -q",
        passed=False,
        exit_code=1,
    )
    assert status == "synced", status
    logged = argv_log.read_text().splitlines()
    # Canonical-DB targeting unchanged: --workspace-root still points
    # at the primary so the row lands in the shared handoff DB.
    assert "--workspace-root" in logged
    assert logged[logged.index("--workspace-root") + 1] == str(primary)
    # Actor overrides reflect the worktree under test.
    assert "--branch" in logged, f"missing --branch in argv: {logged!r}"
    assert logged[logged.index("--branch") + 1] == "feature/WORKSTATE-77"
    assert "--commit-sha" in logged, f"missing --commit-sha in argv: {logged!r}"
    assert logged[logged.index("--commit-sha") + 1] == linked_head


def test_project_test_result_returns_spooled_when_cli_rejects_payload(
    projection, fake_cli, repo_root: Path
) -> None:
    """WORKSTATE-REF-52 implementation note: distinguish ``spooled`` (CLI ran, rejected) from ``pending`` (CLI unreachable).

    A CLI exiting non-zero with a real returncode means the projection was *attempted* and
    the payload was spooled for replay. The receipt status must surface that as
    ``"spooled"``, not ``"pending"`` (which is reserved for CLI-unreachable cases).
    """
    fake_cli('#!/usr/bin/env bash\necho "rejected" 1>&2\nexit 2\n')
    status = projection.project_test_result(
        repo_root,
        session="s",
        command="pytest -q",
        passed=False,
        exit_code=1,
        result="1 failed",
    )
    assert status == "spooled", (
        f"CLI exited non-zero (returncode=2): expected 'spooled', got {status!r}"
    )
    spool = repo_root / PENDING_REL
    assert spool.exists()
    payload = json.loads(spool.read_text().splitlines()[-1])
    assert payload["kind"] == "test_result"


def test_project_test_result_returns_pending_when_cli_missing(
    projection, repo_root: Path
) -> None:
    """WORKSTATE-REF-52 implementation note: ``pending`` is reserved for CLI-unreachable cases."""
    os.environ["MCP_WORKSTATE_HANDOFF_BIN"] = "/nonexistent/path/no-such-binary-xyz"
    try:
        status = projection.project_test_result(
            repo_root,
            session="s",
            command="pytest -q",
            passed=False,
        )
    finally:
        os.environ.pop("MCP_WORKSTATE_HANDOFF_BIN", None)
    assert status == "pending", (
        f"CLI missing (returncode=127): expected 'pending', got {status!r}"
    )
    assert (repo_root / PENDING_REL).exists()


def test_project_test_result_spools_worktree_provenance_on_failure(
    projection, fake_cli, tmp_path: Path
) -> None:
    """implementation note.2.1: when the adapter fails the spooled payload must
    persist the worktree branch + commit_sha so a later replay can
    re-issue the event with matching --branch/--commit-sha actor
    overrides. Regression for finding 152
    (codex_WORKSTATE40_slice4_2_replay_drops_test_provenance).
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=primary, check=True)
    subprocess.run(
        [
            "git", "-C", str(primary),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "--allow-empty", "-m", "init", "-q",
        ],
        check=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        [
            "git", "-C", str(primary),
            "worktree", "add", "-q", "-b", "feature/WORKSTATE-99", str(linked),
        ],
        check=True,
    )
    linked_head = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    fake_cli('#!/usr/bin/env bash\nexit 1\n')

    status = projection.project_test_result(
        linked,
        session="claude_offline_session",
        command="pytest -q",
        passed=False,
        exit_code=1,
    )
    # WORKSTATE-REF-52 implementation note: CLI ran (exit 1) and rejected -> ``spooled``.
    assert status == "spooled"
    spool = linked / PENDING_REL
    payload = json.loads(spool.read_text().splitlines()[-1])
    assert payload["kind"] == "test_result"
    assert payload["branch"] == "feature/WORKSTATE-99"
    assert payload["commit_sha"] == linked_head


def test_project_decision_rejects_blank_decision_id(
    projection, repo_root: Path
) -> None:
    status, returned = projection.project_decision(
        repo_root,
        decision_id="",
        rationale="r",
        session="s",
    )
    assert status == "error"
    assert returned is None
    # An error payload short-circuits before the adapter is invoked, so
    # the spool stays absent.
    assert not (repo_root / PENDING_REL).exists()


# ---------------------------------------------------------------------------
# WS-TASKSTART review follow-up: the exit-0-on-``ok:false`` masking is a
# property of the handoff CLI, not of the ``set`` subcommand. The same guard
# now applies to the sibling event-record projections so a rejected
# ``record_event`` cannot masquerade as synced either.
# ---------------------------------------------------------------------------


def test_project_decision_zero_exit_ok_false_is_spooled(
    projection, fake_cli, repo_root: Path
) -> None:
    """A zero-exit ``{"ok": false}`` from ``record_event`` decision must
    spool and return ``("spooled", None)``, not a false synced decision id.
    """
    fake_cli(
        '#!/usr/bin/env bash\n'
        'echo \'{"ok":false,"data":{"error":"rejected"}}\'\n'
        'exit 0\n'
    )
    status, returned = projection.project_decision(
        repo_root,
        decision_id="claude_zero_exit_reject",
        rationale="r",
        session="s",
    )
    assert status == "spooled", status
    assert returned is None
    spool = repo_root / PENDING_REL
    assert spool.exists()
    assert json.loads(spool.read_text().splitlines()[-1])["kind"] == "decision"


def test_project_test_result_zero_exit_ok_false_is_spooled(
    projection, fake_cli, repo_root: Path
) -> None:
    """A zero-exit ``{"ok": false}`` from ``record_event`` test_result must
    spool and report ``spooled`` rather than a false synced.
    """
    fake_cli(
        '#!/usr/bin/env bash\n'
        'echo \'{"ok":false,"data":{"error":"rejected"}}\'\n'
        'exit 0\n'
    )
    status = projection.project_test_result(
        repo_root,
        session="s",
        command="pytest -q",
        passed=True,
    )
    assert status == "spooled", status
    spool = repo_root / PENDING_REL
    assert spool.exists()
    assert json.loads(spool.read_text().splitlines()[-1])["kind"] == "test_result"


def test_rejection_data_fail_open_contract(projection) -> None:
    """``_rejection_data`` underpins the exit-0 guard: it must return the
    rejection ``data`` ONLY for an explicit ``ok is False`` dict, and
    fail-open (None) on every other shape so legitimate / stub outputs
    (``ok:true``, ``ok`` absent, bare ``echo ok``, JSON lists) stay synced.
    """
    rd = projection._rejection_data
    # Rejections -> data dict (possibly empty).
    assert rd('{"ok": false, "data": {"current_revision": 2}}') == {"current_revision": 2}
    assert rd('{"ok": false}') == {}
    assert rd('{"ok": false, "data": "not-a-dict"}') == {}
    # Non-rejections / fail-open -> None.
    assert rd('{"ok": true, "data": {"inserted": true}}') is None
    assert rd('{"data": {"x": 1}}') is None  # ok absent
    assert rd("ok") is None  # bare stub echo, unparseable
    assert rd("") is None
    assert rd("[1, 2, 3]") is None  # JSON, but not a dict
