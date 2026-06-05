"""implementation note.2 contract tests for the ``close-check`` subcommand.

The handler is the local merge gate: it runs every ``review-ready``
check plus close-only additions (no unresolved blockers, branch
mergeable to base, required close decision exists). Sub-implementation note.2
lands the foundation: review-ready check reuse + a ``mergeable`` flag
that exposes whether the branch can fast-forward / three-way merge to
the base without conflict.

Receipt extras:

* ``ready``: ``bool``
* ``reasons``: ``list[str]``
* ``findings_open``: ``{high, medium, low}``
* ``mergeable``: ``bool`` — close-check only
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PAYLOAD_ROOT = PACKAGE_ROOT / "workstate_system" / "payload"
LIFECYCLE_PKG = PAYLOAD_ROOT / "scripts" / "workstate" / "lifecycle"
CONTRACT_SOURCE = PAYLOAD_ROOT / "docs" / "workstate" / "contracts" / "harness-protocol.yaml"


def _seed_contract(repo: Path) -> None:
    """Copy the harness-protocol contract into the fixture repo so the
    branch-isolation policy loader (used by the dirty-main probe) can
    resolve protected paths. Without it the probe degrades to a clean
    shape and the gate never reports ``dirty_main_protected_paths``."""
    target = repo / "docs" / "workstate" / "contracts" / "harness-protocol.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CONTRACT_SOURCE, target)


def _seed_hook_helpers(repo: Path, *, layout: str = "monorepo") -> None:
    """The dirty-main probe imports ``_branch_isolation_guard`` and
    ``_harness_protocol`` from a hooks directory. Mirror those helpers
    into the fixture repo so the integration test runs the real probe
    path. ``layout="monorepo"`` (default) seeds them under
    ``packages/workstate-system/scripts/hooks``; ``layout="consumer"``
    seeds them under the consumer/bootstrapped path
    ``scripts/hooks`` so the probe's hook-dir resolver is exercised."""
    src = PAYLOAD_ROOT / "scripts" / "hooks"
    if layout == "consumer":
        dest = repo / "scripts" / "hooks"
    else:
        dest = repo / "packages" / "workstate-system" / "scripts" / "hooks"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("_branch_isolation_guard.py", "_harness_protocol.py"):
        shutil.copy2(src / name, dest / name)

REQUIRED_FIELDS = (
    "ok",
    "command",
    "task_ref",
    "branch",
    "worktree_path",
    "head",
    "handoff_projection",
    "events",
)
CLOSE_CHECK_EXTRA_FIELDS = ("ready", "reasons", "findings_open", "mergeable", "warnings")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "--allow-empty", "-m", "init", "-q",
        ],
        check=True,
    )
    return repo


def _checkout_feature(repo: Path, branch: str = "feature/WORKSTATE-21") -> None:
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "-b", branch],
        check=True,
    )


def _commit(repo: Path, path: str, content: str, msg: str) -> None:
    (repo / path).write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", path], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", msg, "-q",
        ],
        check=True,
    )


def _run_close_check(
    cwd: Path,
    *extra: str,
    fake_cli: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = (
        str(fake_cli) if fake_cli is not None else "/nonexistent/no-such-binary-xyz"
    )
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "close-check", *extra],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _write_fake_cli(target: Path, body: str) -> None:
    target.write_text(body)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_close_check_clean_branch_passes(git_repo: Path) -> None:
    """implementation note.2: clean feature branch ahead of main and mergeable →
    ``ready: true``, ``reasons: []``, ``mergeable: true``.
    """
    _checkout_feature(git_repo)
    _commit(git_repo, "x.txt", "hello\n", "feat: x")

    proc = _run_close_check(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    for field in (*REQUIRED_FIELDS, *CLOSE_CHECK_EXTRA_FIELDS):
        assert field in receipt, f"missing field {field!r}: {receipt!r}"
    assert receipt["command"] == "close-check"
    assert receipt["ready"] is True, receipt
    assert receipt["reasons"] == [], receipt
    assert receipt["mergeable"] is True, receipt


def test_close_check_inherits_review_ready_protected_base(git_repo: Path) -> None:
    """implementation note.2: close-check inherits every review-ready check, so
    running on ``main`` yields ``on_protected_base``.
    """
    proc = _run_close_check(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert "on_protected_base" in receipt["reasons"], receipt


def test_close_check_unmergeable_branch_blocks(git_repo: Path) -> None:
    """implementation note.2: when the feature branch and main both edit the same
    region of the same file, ``mergeable`` is false and ``reasons``
    carries ``"unmergeable"``.
    """
    # Seed shared file on main.
    _commit(git_repo, "shared.txt", "base\n", "chore: seed shared.txt")

    # Feature branch edits the file.
    _checkout_feature(git_repo)
    _commit(git_repo, "shared.txt", "feature edit\n", "feat: edit shared.txt")

    # Now switch back to main and conflict.
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "main"], check=True
    )
    _commit(git_repo, "shared.txt", "main edit\n", "chore: edit shared.txt on main")

    # Switch back to the feature branch for the close-check run.
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "feature/WORKSTATE-21"],
        check=True,
    )

    proc = _run_close_check(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["mergeable"] is False, receipt
    assert "unmergeable" in receipt["reasons"], receipt
    assert receipt["ready"] is False, receipt


def test_close_check_inherits_findings_open_from_handoff_cli(
    git_repo: Path, tmp_path: Path
) -> None:
    """implementation note.4: close-check must surface the same ``findings_open``
    bucket and ``open_high_finding`` / ``open_medium_finding`` reasons
    as review-ready. Previously close-check went through the default
    ``_emit`` path and reported zero counts even when the handoff CLI
    listed open HIGH findings — operators saw a green-ish merge gate
    while review-ready blocked, masking the violation.
    """
    _checkout_feature(git_repo)
    _commit(git_repo, "x.txt", "hello\n", "feat: x")

    fake_cli = tmp_path / "fake-mcp"
    canned = json.dumps(
        {
            "ok": True,
            "data": {
                "counts": {
                    "severity": {"high": 1, "medium": 0, "low": 0},
                    "status": {"open": 1},
                },
                "findings": [],
            },
        }
    )
    body = (
        "#!/usr/bin/env bash\n"
        'for arg in "$@"; do\n'
        '  if [[ "$arg" == "--workspace-root" ]]; then\n'
        f"    cat <<'EOF'\n{canned}\nEOF\n"
        "    exit 0\n"
        "  fi\n"
        "done\n"
        "exit 2\n"
    )
    _write_fake_cli(fake_cli, body)

    proc = _run_close_check(git_repo, "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["findings_open"] == {"high": 1, "medium": 0, "low": 0}, receipt
    assert "open_high_finding" in receipt["reasons"], receipt
    assert receipt["ready"] is False, receipt
    assert receipt["handoff_projection"] == "synced", receipt


def test_close_check_text_mode_prints_status(git_repo: Path) -> None:
    """Without ``--json`` close-check prints the same READY / NOT READY
    one-liner shape review-ready does.
    """
    _checkout_feature(git_repo)
    proc = _run_close_check(git_repo)
    assert proc.returncode == 0, proc.stderr
    assert "NOT READY" in proc.stderr, proc.stderr


# WORKSTATE-REF-53 implementation note: close-check / review-ready receipts must include
# next-command guidance and reason ownership so the operator stops
# inspecting the dashboard or sqlite for routine readiness.
def test_close_check_includes_next_command_when_ready(git_repo: Path) -> None:
    _checkout_feature(git_repo)
    _commit(git_repo, "x.txt", "hello\n", "feat: x")

    proc = _run_close_check(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is True, receipt
    assert "next_command" in receipt, receipt
    assert isinstance(receipt["next_command"], dict)
    assert "command" in receipt["next_command"]
    assert "reason" in receipt["next_command"]
    assert receipt["next_command"]["reason"] == "branch_ready_to_merge", receipt


# WORKSTATE-REF-53 implementation note (finding WORKSTATE53-S5-BR-01): close-check next_command
# must substitute the derived task_ref so the ready-to-merge hint is a
# copy-pasteable command, not a literal placeholder.
def test_close_check_next_command_substitutes_derived_task_ref_when_ready(
    git_repo: Path,
) -> None:
    _checkout_feature(git_repo)
    _commit(git_repo, "x.txt", "hello\n", "feat: x")

    proc = _run_close_check(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is True, receipt
    assert receipt["task_ref"] == "WORKSTATE-REF-21", receipt
    cmd = receipt["next_command"]["command"]
    assert "<task-ref>" not in cmd, receipt
    assert "TASK=WORKSTATE-REF-21" in cmd, receipt


def test_close_check_includes_next_command_when_blocked(git_repo: Path) -> None:
    """When close-check rejects, next_command should point at the canonical
    fix surface rather than nudging the operator to inspect raw artifacts."""
    proc = _run_close_check(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert "next_command" in receipt
    assert receipt["next_command"]["command"]
    assert receipt["next_command"]["reason"]


def test_close_check_groups_reasons_by_owner(git_repo: Path) -> None:
    """WORKSTATE-REF-53 implementation note: close-check failures must be tagged with an
    owner bucket (feature_branch / handoff_evidence / mergeability /
    root_main_hygiene) so the operator can route the fix without
    reading the test source."""
    proc = _run_close_check(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "reasons_by_owner" in receipt, receipt
    grouped = receipt["reasons_by_owner"]
    for bucket in ("feature_branch", "handoff_evidence", "mergeability", "root_main_hygiene"):
        assert bucket in grouped, f"missing bucket {bucket!r}: {grouped!r}"
        assert isinstance(grouped[bucket], list)
    # ``on_protected_base`` is a feature-branch concern.
    assert "on_protected_base" in grouped["feature_branch"], grouped


def test_close_check_emits_dirty_main_protected_paths_reason(tmp_path: Path) -> None:
    """WORKSTATE-REF-53 implementation note (finding WORKSTATE53-S4-BR-01): close-check must
    refuse ``ready`` when the canonical workspace has dirty protected
    paths on a protected base.

    implementation note retuned post-merge to warn-only; the close gate has to
    pick up that contract gap so an operator running close-check can't
    declare the branch ready while root main carries uncommitted
    protected-path drift. Reason ``dirty_main_protected_paths`` was
    already wired through ``_REASON_OWNER`` to the
    ``root_main_hygiene`` bucket; this test pins the producer.
    """
    repo = tmp_path / "primary"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    _seed_contract(repo)
    _seed_hook_helpers(repo)
    # Seed a protected ``.py`` file under the code-roots so it is a
    # protected path per the harness contract.
    (repo / "packages" / "workstate-system" / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / "packages" / "workstate-system" / "scripts" / "guarded.py").write_text("# baseline\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "-m", "init",
        ],
        check=True,
    )
    # Dirty the protected path on main without committing.
    (repo / "packages" / "workstate-system" / "scripts" / "guarded.py").write_text("# dirty\n")

    proc = _run_close_check(repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "dirty_main_protected_paths" in receipt["reasons"], receipt
    assert "dirty_main_protected_paths" in receipt["reasons_by_owner"]["root_main_hygiene"], receipt
    assert receipt["ready"] is False, receipt


def test_close_check_emits_dirty_main_protected_paths_reason_in_consumer_layout(
    tmp_path: Path,
) -> None:
    """A bootstrapped consumer layout puts hook helpers under
    ``scripts/hooks`` (no ``packages/workstate-system`` wrapper). The
    dirty-main probe must locate those helpers via a shared resolver
    that supports both layouts; otherwise close-check silently returns
    clean and lets dirty protected paths slip through to merge."""
    repo = tmp_path / "consumer"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    _seed_contract(repo)
    _seed_hook_helpers(repo, layout="consumer")
    # Seed and dirty a protected ``.py`` file under the code-roots so it
    # is a protected path per the harness contract.
    (repo / "packages" / "workstate-system" / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / "packages" / "workstate-system" / "scripts" / "guarded.py").write_text("# baseline\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "-m", "init",
        ],
        check=True,
    )
    (repo / "packages" / "workstate-system" / "scripts" / "guarded.py").write_text("# dirty\n")

    proc = _run_close_check(repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "dirty_main_protected_paths" in receipt["reasons"], receipt
    assert receipt["ready"] is False, receipt


def test_close_check_does_not_emit_dirty_main_when_main_clean(tmp_path: Path) -> None:
    """The dirty-main probe must stay quiet when the canonical
    workspace's protected paths are clean. Without this guard the
    probe could false-positive on every clean close-check run."""
    repo = tmp_path / "primary"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    _seed_contract(repo)
    _seed_hook_helpers(repo)
    (repo / "packages" / "workstate-system" / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / "packages" / "workstate-system" / "scripts" / "guarded.py").write_text("# baseline\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "-m", "init",
        ],
        check=True,
    )

    proc = _run_close_check(repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "dirty_main_protected_paths" not in receipt["reasons"], receipt
    assert receipt["reasons_by_owner"]["root_main_hygiene"] == [], receipt


def test_close_check_inherits_orphan_planning_warning(git_repo: Path) -> None:
    """WORKSTATE-REF-50 implementation note: close-check uses the shared ``review_ready._emit``
    path, so the warn-only orphan-planning-artifact entry threads through
    untouched. Untracked planning file on a feature branch ⇒ ``warnings``
    populated, ``mergeable`` and ``reasons`` semantics unchanged.
    """
    _checkout_feature(git_repo)
    _commit(git_repo, "x.txt", "hello\n", "feat: x")
    (git_repo / "docs" / "adrs").mkdir(parents=True)
    (git_repo / "docs" / "adrs" / "00-orphan.md").write_text("# orphan\n")

    proc = _run_close_check(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert "warnings" in receipt, receipt
    assert any("docs/adrs/00-orphan.md" in w for w in receipt["warnings"]), receipt
    assert receipt["command"] == "close-check"
