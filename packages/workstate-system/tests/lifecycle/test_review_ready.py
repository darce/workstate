"""implementation note contract tests for the ``review-ready`` subcommand.

The handler is the local pre-review gate: it inspects the current git
context plus handoff state and emits a stable JSON receipt indicating
whether the branch is ready for review. Each sub-slice lands one
check; later sub-slices extend the same receipt.

Receipt extras (per ``§JSON Receipt Schema`` in implementation note):

* ``ready``: ``bool`` — overall verdict
* ``reasons``: ``list[str]`` — short tokens describing why the gate
  rejected (empty when ``ready`` is true)
* ``findings_open``: ``{high: int, medium: int, low: int}`` — open
  finding counts; populated in 5.3, defaults to all-zero earlier.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


def _write_fake_cli(target: Path, body: str) -> None:
    target.write_text(body)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "workstate_system" / "payload" / "scripts" / "workstate" / "lifecycle"

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
REVIEW_READY_EXTRA_FIELDS = ("ready", "reasons", "findings_open", "warnings")


def _write_split_fake_cli(
    target: Path,
    *,
    findings_json: str,
    tests_json: str,
) -> None:
    """Fake CLI that dispatches by subcommand: review-findings vs
    get-verified-tests. Mirrors the real argparse shape (parent
    ``--workspace-root`` + subparsers) so flag-order regressions cannot
    pass.
    """
    target.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, json, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--workspace-root', required=True)\n"
        "sub = p.add_subparsers(dest='subcommand', required=True)\n"
        "rf = sub.add_parser('review-findings')\n"
        "rf.add_argument('--operation', required=True)\n"
        "rf.add_argument('--status', default='all')\n"
        "rf.add_argument('--task-ref', default=None)\n"
        "vt = sub.add_parser('get-verified-tests')\n"
        "vt.add_argument('--task-ref', default=None)\n"
        "vt.add_argument('--passed', default=None)\n"
        "vt.add_argument('--exclude-never-passed', action='store_true')\n"
        "vt.add_argument('--limit', type=int, default=100)\n"
        "st = sub.add_parser('state')\n"
        "st.add_argument('task_ref', nargs='?', default=None)\n"
        "st.add_argument('--sections', default=None)\n"
        "st.add_argument('--detail', default='full')\n"
        "st.add_argument('--verbose', action='store_true')\n"
        "args = p.parse_args()\n"
        "if args.subcommand == 'review-findings':\n"
        f"    sys.stdout.write({findings_json!r})\n"
        "elif args.subcommand == 'get-verified-tests':\n"
        f"    sys.stdout.write({tests_json!r})\n"
        "elif args.subcommand == 'state':\n"
        "    sys.stdout.write('{\"ok\": true, \"data\": {\"active\": null}}')\n"
    )
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _empty_findings_payload() -> str:
    return json.dumps(
        {
            "ok": True,
            "data": {
                "counts": {
                    "severity": {"high": 0, "medium": 0, "low": 0},
                    "status": {"open": 0},
                },
                "findings": [],
            },
        }
    )


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


def _run_review_ready(
    cwd: Path,
    *extra: str,
    fake_cli: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = (
        str(fake_cli) if fake_cli is not None else "/nonexistent/no-such-binary-xyz"
    )
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "review-ready", *extra],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _checkout_feature(repo: Path, branch: str = "feature/WORKSTATE-21") -> None:
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "-b", branch],
        check=True,
    )


def test_review_ready_clean_feature_branch_passes(
    git_repo: Path, tmp_path: Path
) -> None:
    """implementation note.1: clean feature branch with one commit ahead of main →
    ``ready: true`` and ``reasons: []``.
    """
    _checkout_feature(git_repo)
    # Add a commit so the branch is meaningfully ahead of main.
    (git_repo / "x.txt").write_text("hello\n")
    subprocess.run(
        ["git", "-C", str(git_repo), "add", "x.txt"], check=True
    )
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", "feat: x", "-q",
        ],
        check=True,
    )

    proc = _run_review_ready(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    for field in (*REQUIRED_FIELDS, *REVIEW_READY_EXTRA_FIELDS):
        assert field in receipt, f"missing field {field!r}: {receipt!r}"
    assert receipt["ok"] is True
    assert receipt["command"] == "review-ready"
    assert receipt["ready"] is True, receipt
    assert receipt["reasons"] == [], receipt
    assert receipt["findings_open"] == {"high": 0, "medium": 0, "low": 0}
    assert receipt["task_ref"] == "WORKSTATE-REF-21"


def test_review_ready_protected_base_blocks(
    git_repo: Path, tmp_path: Path
) -> None:
    """implementation note.1: running on ``main`` yields ``ready: false`` with
    ``"on_protected_base"`` in ``reasons``.
    """
    proc = _run_review_ready(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert "on_protected_base" in receipt["reasons"], receipt


def test_review_ready_dirty_worktree_blocks(
    git_repo: Path, tmp_path: Path
) -> None:
    """implementation note.1: feature branch with uncommitted changes → ``ready:
    false`` with ``"dirty_worktree"`` in ``reasons``.
    """
    _checkout_feature(git_repo)
    (git_repo / "x.txt").write_text("hello\n")
    subprocess.run(
        ["git", "-C", str(git_repo), "add", "x.txt"], check=True
    )
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", "feat: x", "-q",
        ],
        check=True,
    )
    # Now dirty the tree.
    (git_repo / "x.txt").write_text("dirty\n")

    proc = _run_review_ready(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert "dirty_worktree" in receipt["reasons"], receipt


def test_review_ready_no_commits_ahead_blocks(
    git_repo: Path, tmp_path: Path
) -> None:
    """implementation note.1: feature branch with zero commits ahead of main and
    no diff → ``ready: false`` with ``"no_changes_against_base"`` in
    ``reasons``.
    """
    _checkout_feature(git_repo)
    proc = _run_review_ready(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert "no_changes_against_base" in receipt["reasons"], receipt


def test_review_ready_zero_commits_with_advanced_base_blocks(
    git_repo: Path, tmp_path: Path
) -> None:
    """Regression for finding BR-WORKSTATE40-S5-01: a feature branch with
    zero commits of its own must be classified as ``no_changes_against_base``
    even when ``main`` has advanced since the branch was cut. The
    earlier implementation fell back to ``git diff --quiet base HEAD``,
    which inverted the truth when base moved forward — main's new files
    were absent on the branch HEAD, so the diff was non-empty and the
    branch was misclassified as ready.
    """
    _checkout_feature(git_repo)
    # Advance main beyond the branch point.
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "main"], check=True
    )
    (git_repo / "advanced.txt").write_text("base advanced\n")
    subprocess.run(
        ["git", "-C", str(git_repo), "add", "advanced.txt"], check=True
    )
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", "chore: advance main", "-q",
        ],
        check=True,
    )
    # Back to the feature branch (still zero commits of its own).
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "feature/WORKSTATE-21"],
        check=True,
    )

    proc = _run_review_ready(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert "no_changes_against_base" in receipt["reasons"], receipt


def test_review_ready_text_mode_prints_status(
    git_repo: Path, tmp_path: Path
) -> None:
    """Without ``--json`` the handler prints a one-line ``READY`` /
    ``NOT READY: <reasons>`` header to stderr while still emitting the
    JSON receipt to stdout (so machine consumers and humans can both
    read the same run).
    """
    _checkout_feature(git_repo)
    proc = _run_review_ready(git_repo)
    assert proc.returncode == 0, proc.stderr
    assert "NOT READY" in proc.stderr, proc.stderr
    assert "no_changes_against_base" in proc.stderr, proc.stderr


def _commit_one(repo: Path) -> None:
    """Add one commit so the feature branch is ahead of main."""
    (repo / "x.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "x.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", "feat: x", "-q",
        ],
        check=True,
    )


def test_review_ready_findings_open_populated_from_handoff_cli(
    git_repo: Path, tmp_path: Path
) -> None:
    """implementation note.3: review-ready shells out to ``mcp-workstate-handoff
    review-findings --operation list --status open`` and populates
    ``findings_open`` from the response's ``data.counts.severity``
    bucket. When any HIGH finding is open, the receipt also carries
    ``"open_high_finding"`` in ``reasons``.
    """
    _checkout_feature(git_repo)
    _commit_one(git_repo)

    fake_cli = tmp_path / "fake-mcp"
    canned = json.dumps(
        {
            "ok": True,
            "data": {
                "counts": {
                    "severity": {"high": 2, "medium": 1, "low": 0},
                    "status": {"open": 3},
                },
                "findings": [],
            },
        }
    )
    _write_fake_cli(
        fake_cli,
        f'#!/usr/bin/env bash\ncat <<\'EOF\'\n{canned}\nEOF\nexit 0\n',
    )

    proc = _run_review_ready(git_repo, "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["findings_open"] == {"high": 2, "medium": 1, "low": 0}
    assert "open_high_finding" in receipt["reasons"], receipt
    assert receipt["ready"] is False, receipt


def test_review_ready_findings_open_zero_when_handoff_offline(
    git_repo: Path, tmp_path: Path
) -> None:
    """implementation note.3: when the handoff CLI is unavailable, ``findings_open``
    stays all-zero, no finding-related reason is added, and the
    receipt's ``handoff_projection`` flips to ``"pending"`` so consumers
    know the count is unverified rather than confirmed empty.
    """
    _checkout_feature(git_repo)
    _commit_one(git_repo)
    proc = _run_review_ready(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["findings_open"] == {"high": 0, "medium": 0, "low": 0}
    assert "open_high_finding" not in receipt["reasons"], receipt
    assert "open_medium_finding" not in receipt["reasons"], receipt
    assert receipt["handoff_projection"] == "pending", receipt


def test_review_ready_workspace_root_precedes_subcommand(
    git_repo: Path, tmp_path: Path
) -> None:
    """Regression for finding BR-WORKSTATE40-S5-04-01: ``--workspace-root``
    is a *global* flag on the real ``mcp-workstate-handoff`` argparser
    (registered on the parent parser before ``add_subparsers``). Putting
    it after the subcommand causes the real adapter to exit 2 with
    ``unrecognized arguments``.

    The fake CLI here mirrors the real argparser shape so flag-order
    mistakes cannot pass with bash stubs that just scan argv. If
    ``--workspace-root`` is positioned after ``review-findings``,
    argparse rejects the call and the gate degrades to
    ``handoff_projection="pending"`` with zero counts.
    """
    _checkout_feature(git_repo)
    _commit_one(git_repo)

    fake_cli = tmp_path / "fake-mcp-real-argparse"
    findings_payload = json.dumps(
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
    tests_payload = json.dumps({"ok": True, "data": {"tests": []}})
    _write_split_fake_cli(
        fake_cli,
        findings_json=findings_payload,
        tests_json=tests_payload,
    )

    proc = _run_review_ready(git_repo, "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["handoff_projection"] == "synced", receipt
    assert receipt["findings_open"] == {"high": 1, "medium": 0, "low": 0}, receipt
    assert "open_high_finding" in receipt["reasons"], receipt


def test_review_ready_passes_workspace_root_to_handoff_cli(
    git_repo: Path, tmp_path: Path
) -> None:
    """Regression for finding BR-WORKSTATE40-S5-03-01: ``review-ready`` must
    invoke ``mcp-workstate-handoff review-findings`` with
    ``--workspace-root`` so the real CLI does not exit with
    ``WORKSTATE_HANDOFF_WORKSPACE_ROOT must be set``. Without this flag the
    gate silently degrades to ``handoff_projection="pending"`` with zero
    counts and never enforces open HIGH/MEDIUM findings.

    The fake CLI here exits non-zero unless ``--workspace-root`` is
    present. A passing run therefore proves the flag is threaded
    through, and a real HIGH count is surfaced (not zero from the
    pending fallback).
    """
    _checkout_feature(git_repo)
    _commit_one(git_repo)

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
    # Fake CLI: require --workspace-root in argv; otherwise exit 2 with
    # the same RuntimeError-style message the real adapter raises.
    body = (
        "#!/usr/bin/env bash\n"
        'for arg in "$@"; do\n'
        '  if [[ "$arg" == "--workspace-root" ]]; then\n'
        f"    cat <<'EOF'\n{canned}\nEOF\n"
        "    exit 0\n"
        "  fi\n"
        "done\n"
        'echo "WORKSTATE_HANDOFF_WORKSPACE_ROOT must be set or passed via --workspace-root" 1>&2\n'
        "exit 2\n"
    )
    _write_fake_cli(fake_cli, body)

    proc = _run_review_ready(git_repo, "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["handoff_projection"] == "synced", receipt
    assert receipt["findings_open"] == {"high": 1, "medium": 0, "low": 0}, receipt
    assert "open_high_finding" in receipt["reasons"], receipt


def test_review_ready_open_medium_finding_when_only_medium(
    git_repo: Path, tmp_path: Path
) -> None:
    """implementation note.3: a single open MEDIUM finding (no HIGH) yields the
    ``"open_medium_finding"`` reason without ``"open_high_finding"``.
    """
    _checkout_feature(git_repo)
    _commit_one(git_repo)

    fake_cli = tmp_path / "fake-mcp"
    canned = json.dumps(
        {
            "ok": True,
            "data": {
                "counts": {
                    "severity": {"high": 0, "medium": 1, "low": 0},
                    "status": {"open": 1},
                },
                "findings": [],
            },
        }
    )
    _write_fake_cli(
        fake_cli,
        f'#!/usr/bin/env bash\ncat <<\'EOF\'\n{canned}\nEOF\nexit 0\n',
    )

    proc = _run_review_ready(git_repo, "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["findings_open"] == {"high": 0, "medium": 1, "low": 0}
    assert "open_medium_finding" in receipt["reasons"], receipt
    assert "open_high_finding" not in receipt["reasons"], receipt


def _head_sha(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def test_review_ready_stale_test_evidence_reason_when_sha_mismatch(
    git_repo: Path, tmp_path: Path
) -> None:
    """implementation note.5: when the most recent passing verified_test was
    recorded against a commit other than the current HEAD, review-ready
    adds ``stale_test_evidence`` to ``reasons`` and ``ready`` is false.
    The fake CLI returns a verified_test row whose ``commit_sha`` is a
    placeholder unrelated to the local HEAD.
    """
    _checkout_feature(git_repo)
    _commit_one(git_repo)

    fake_cli = tmp_path / "fake-mcp"
    tests_json = json.dumps(
        {
            "ok": True,
            "data": {
                "tests": [
                    {
                        "id": 1,
                        "commit_sha": "0000000000000000000000000000000000000000",
                        "passed": True,
                        "command": "pytest",
                    }
                ],
                "total_matching": 1,
            },
        }
    )
    _write_split_fake_cli(
        fake_cli,
        findings_json=_empty_findings_payload(),
        tests_json=tests_json,
    )

    proc = _run_review_ready(git_repo, "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "stale_test_evidence" in receipt["reasons"], receipt
    assert receipt["ready"] is False, receipt


def test_review_ready_fresh_test_evidence_no_reason(
    git_repo: Path, tmp_path: Path
) -> None:
    """implementation note.5: when the most recent passing verified_test
    ``commit_sha`` matches the current HEAD, review-ready does NOT add
    ``stale_test_evidence`` and the branch passes the gate (assuming no
    other failing checks).
    """
    _checkout_feature(git_repo)
    _commit_one(git_repo)
    head = _head_sha(git_repo)

    fake_cli = tmp_path / "fake-mcp"
    tests_json = json.dumps(
        {
            "ok": True,
            "data": {
                "tests": [
                    {"id": 1, "commit_sha": head, "passed": True, "command": "pytest"}
                ],
                "total_matching": 1,
            },
        }
    )
    _write_split_fake_cli(
        fake_cli,
        findings_json=_empty_findings_payload(),
        tests_json=tests_json,
    )

    proc = _run_review_ready(git_repo, "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "stale_test_evidence" not in receipt["reasons"], receipt
    assert receipt["ready"] is True, receipt


def test_review_ready_no_test_evidence_when_handoff_offline(
    git_repo: Path, tmp_path: Path
) -> None:
    """implementation note.5: when the handoff CLI is unavailable, ``review-ready``
    does NOT add ``stale_test_evidence`` (we cannot tell whether evidence
    is fresh or stale, so don't penalize). ``handoff_projection`` is
    ``"pending"`` to flag the unverified state.
    """
    _checkout_feature(git_repo)
    _commit_one(git_repo)

    proc = _run_review_ready(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "stale_test_evidence" not in receipt["reasons"], receipt
    assert receipt["handoff_projection"] == "pending", receipt


# ---------------------------------------------------------------------------
# WORKSTATE-REF-50 implementation note: warn-only orphan-planning-artifact check
# ---------------------------------------------------------------------------


def _orphan_warning_text(receipt: dict) -> str:
    return "\n".join(receipt.get("warnings", []))


def test_review_ready_orphan_planning_artifact_emits_warning_on_main(
    git_repo: Path, tmp_path: Path
) -> None:
    """WORKSTATE-REF-50 implementation note: an untracked planning artifact on ``main``
    appends a warn-only entry. ``ready`` and ``reasons`` only reflect
    pre-existing checks (here: ``on_protected_base``); the warning is
    additive.
    """
    (git_repo / "docs" / "scopes").mkdir(parents=True)
    (git_repo / "docs" / "scopes" / "orphan-note.md").write_text("scratch\n")

    proc = _run_review_ready(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert "warnings" in receipt, receipt
    assert any("docs/scopes/orphan-note.md" in w for w in receipt["warnings"]), receipt
    assert any("planning-artifact-home.md" in w for w in receipt["warnings"]), receipt
    # Pre-existing reasons (protected-base, dirty-worktree from the
    # untracked file) may be present; the warning never adds its own
    # reason and ``ready`` is unchanged versus the no-warning case.
    assert "on_protected_base" in receipt["reasons"], receipt
    assert receipt["ready"] is False, receipt


def test_review_ready_orphan_planning_artifact_emits_warning_on_feature(
    git_repo: Path, tmp_path: Path
) -> None:
    """WORKSTATE-REF-50 implementation note: an untracked planning artifact on a feature
    branch is still an orphan — branch identity does not legitimize the
    untracked state. ``warnings`` populated; ``reasons`` unaffected by
    the warning (the dirty_worktree reason kicks in independently
    because the file is in the working tree).
    """
    _checkout_feature(git_repo)
    _commit_one(git_repo)
    (git_repo / "packages").mkdir(parents=True, exist_ok=True)
    pkg_tasks = git_repo / "packages" / "demo-pkg" / "docs" / "tasks"
    pkg_tasks.mkdir(parents=True)
    (pkg_tasks / "DEMO-1-orphan-task-plan.md").write_text("draft\n")

    proc = _run_review_ready(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert any(
        "packages/demo-pkg/docs/tasks/DEMO-1-orphan-task-plan.md" in w
        for w in receipt["warnings"]
    ), receipt
    assert "on_protected_base" not in receipt["reasons"], receipt


def test_review_ready_tracked_planning_artifact_emits_no_warning(
    git_repo: Path, tmp_path: Path
) -> None:
    """WORKSTATE-REF-50 implementation note: a planning artifact that is tracked (committed
    or staged) on a feature branch produces zero warnings — only the
    untracked state is an orphan per the rule's three-state model.
    """
    _checkout_feature(git_repo)
    (git_repo / "docs" / "plans").mkdir(parents=True)
    plan = git_repo / "docs" / "plans" / "0042-real-plan.md"
    plan.write_text("# Plan\n")
    subprocess.run(
        ["git", "-C", str(git_repo), "add", str(plan)],
        check=True,
    )
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", "docs(plans): real plan", "-q",
        ],
        check=True,
    )

    proc = _run_review_ready(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["warnings"] == [], _orphan_warning_text(receipt)


# WORKSTATE-REF-53 implementation note: review-ready receipt includes next-command guidance
# consistent with the canonical workflow loop.
def test_review_ready_includes_next_command_when_ready(
    git_repo: Path, tmp_path: Path
) -> None:
    _checkout_feature(git_repo)
    _commit_one(git_repo)

    proc = _run_review_ready(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is True, receipt
    assert "next_command" in receipt, receipt
    assert receipt["next_command"]["reason"] == "branch_ready_for_close_check", receipt
    assert "make close-check" in receipt["next_command"]["command"], receipt


def test_review_ready_includes_next_command_when_blocked(
    git_repo: Path, tmp_path: Path
) -> None:
    """When review-ready rejects, next_command points at the canonical
    fix surface rather than nudging at raw artifacts."""
    proc = _run_review_ready(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert "next_command" in receipt, receipt
    assert receipt["next_command"]["command"]
    assert receipt["next_command"]["reason"]


# WORKSTATE-REF-53 implementation note (finding WORKSTATE53-S5-BR-01): the next_command guidance
# must substitute the derived task_ref into ``TASK=<task-ref>`` so the
# emitted line is copy-pasteable instead of a literal placeholder.
def test_review_ready_next_command_substitutes_derived_task_ref_when_dirty(
    git_repo: Path, tmp_path: Path
) -> None:
    _checkout_feature(git_repo)
    _commit_one(git_repo)
    # Now dirty the worktree so the feature_branch bucket fires.
    (git_repo / "x.txt").write_text("dirty\n")

    proc = _run_review_ready(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert receipt["task_ref"] == "WORKSTATE-REF-21", receipt
    cmd = receipt["next_command"]["command"]
    assert "<task-ref>" not in cmd, receipt
    assert "TASK=WORKSTATE-REF-21" in cmd, receipt


def test_review_ready_next_command_substitutes_derived_task_ref_when_no_changes(
    git_repo: Path, tmp_path: Path
) -> None:
    _checkout_feature(git_repo)
    proc = _run_review_ready(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert receipt["task_ref"] == "WORKSTATE-REF-21", receipt
    cmd = receipt["next_command"]["command"]
    assert "<task-ref>" not in cmd, receipt
    assert "TASK=WORKSTATE-REF-21" in cmd, receipt


def test_review_ready_next_command_keeps_placeholder_when_task_ref_unknown(
    git_repo: Path, tmp_path: Path
) -> None:
    """On a protected base (``main``), no task_ref is derivable. The
    placeholder ``<task-ref>`` must remain so the operator notices they
    need to provide one rather than running a half-substituted command."""
    proc = _run_review_ready(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert receipt["task_ref"] is None, receipt
    assert "<task-ref>" in receipt["next_command"]["command"], receipt


def test_review_ready_untracked_non_planning_file_emits_no_warning(
    git_repo: Path, tmp_path: Path
) -> None:
    """WORKSTATE-REF-50 implementation note: an untracked file outside the canonical
    planning homes does not trigger the warning. ``warnings`` stays
    empty.
    """
    _checkout_feature(git_repo)
    _commit_one(git_repo)
    (git_repo / "notes.txt").write_text("scratch\n")
    (git_repo / "src").mkdir(exist_ok=True)
    (git_repo / "src" / "untracked.py").write_text("pass\n")

    proc = _run_review_ready(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["warnings"] == [], _orphan_warning_text(receipt)
