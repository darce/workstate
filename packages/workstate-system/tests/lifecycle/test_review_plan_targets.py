"""implementation note contract tests for review/plan-side lifecycle targets.

Two fixture groups:

* **Skill-broadcast**: ``plan-review`` and ``plan-analyze`` produce
  ``ok: true``, ``delegation_mode: "in_session_skill"``,
  ``delegated_to: "skill:<...>"``. When the fake CLI returns non-zero
  the receipt still reports ``ok: true`` with ``intent_event_id: null``
  and a pending event is spooled to
  ``.task-state/pending-workflow-events.jsonl``.
* **Shell-out**: ``review-run``, ``handoff-review-run``, and
  ``handoff-close-check`` invoke the documented argv prefix; success
  returns ``ok: true`` with the documented ``delegated_to`` string;
  missing CLI returns ``ok: false`` and ``delegated_exit_code: 127``.
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
LIFECYCLE_PKG = PACKAGE_ROOT / "scripts" / "workstate" / "lifecycle"

PENDING_EVENTS_REL = Path(".task-state") / "pending-workflow-events.jsonl"

SHELL_OUT_EXPECTED: dict[str, dict[str, object]] = {
    "review-run": {
        "delegated_to": "mcp-workstate-handoff review-runs record --review-mode=branch",
        "argv_contains": ["review-runs", "--review-mode", "branch"],
    },
    "handoff-review-run": {
        "delegated_to": "mcp-workstate-handoff review-runs record --review-mode=planning",
        "argv_contains": ["review-runs", "--review-mode", "planning"],
    },
    "handoff-close-check": {
        "delegated_to": "mcp-workstate-handoff integrity-check --kind close",
        "argv_contains": ["integrity-check", "--kind", "close"],
    },
}


def _write_fake_cli(target: Path, body: str) -> None:
    target.write_text(body)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit",
         "--allow-empty", "-m", "init", "-q"],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


@pytest.fixture
def fake_cli_dir(tmp_path: Path) -> Path:
    return tmp_path / "fake-cli"


def _run_lifecycle(
    cwd: Path, fake_cli: Path, subcommand: str, *extra: str
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = str(fake_cli)
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), subcommand, *extra],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


# ---------- Skill-broadcast ----------


@pytest.mark.parametrize(
    ("command", "skill"),
    [("plan-review", "planning-review"), ("plan-analyze", "plan-analyze")],
)
def test_skill_broadcast_happy_path_records_intent_event(
    git_repo: Path, fake_cli_dir: Path, command: str, skill: str
) -> None:
    fake_cli_dir.mkdir(parents=True)
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    # Fake CLI returns a recognizable JSON receipt with a decision id.
    _write_fake_cli(
        fake_cli,
        '#!/usr/bin/env bash\n'
        'echo \'{"ok":true,"data":{"decision_id":"fake_decision_42"}}\'\n',
    )
    proc = _run_lifecycle(git_repo, fake_cli, command, "--doc", "docs/plan.md", "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["command"] == command
    assert receipt["delegation_mode"] == "in_session_skill"
    assert receipt["delegated_to"] == f"skill:{skill}"
    assert receipt["intent_event_id"] == "fake_decision_42"
    assert receipt["handoff_projection"] == "synced"
    assert not (git_repo / PENDING_EVENTS_REL).exists()


def test_skill_broadcast_mcp_offline_spools_pending_event(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    fake_cli_dir.mkdir(parents=True)
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 1\n")
    proc = _run_lifecycle(git_repo, fake_cli, "plan-analyze", "--doc", "docs/p.md", "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["intent_event_id"] is None
    # WORKSTATE-REF-52 implementation note: ``exit 1`` is CLI ran + rejected → ``spooled``.
    # The CLI-unreachable case (returncode 124/127) keeps reporting
    # ``pending``; this test pins the rejection branch.
    assert receipt["handoff_projection"] == "spooled"
    spool = git_repo / PENDING_EVENTS_REL
    assert spool.exists(), "rejected event must be spooled when MCP rejects"
    pending = json.loads(spool.read_text().splitlines()[0])
    assert pending["kind"] == "workflow_intent"
    assert pending["skill"] == "plan-analyze"
    assert pending["doc"] == "docs/p.md"


# ---------- Shell-out ----------


@pytest.mark.parametrize("command", sorted(SHELL_OUT_EXPECTED))
def test_shell_out_happy_path(
    git_repo: Path, fake_cli_dir: Path, command: str
) -> None:
    fake_cli_dir.mkdir(parents=True)
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    argv_log = fake_cli_dir / "argv.log"
    _write_fake_cli(
        fake_cli,
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {argv_log}\necho ok\nexit 0\n',
    )
    proc = _run_lifecycle(git_repo, fake_cli, command, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    expected = SHELL_OUT_EXPECTED[command]
    assert receipt["ok"] is True
    assert receipt["command"] == command
    assert receipt["delegation_mode"] == "shell_out"
    assert receipt["delegated_to"] == expected["delegated_to"]
    assert receipt["delegated_exit_code"] == 0
    logged_argv = argv_log.read_text().splitlines()
    for token in expected["argv_contains"]:  # type: ignore[union-attr]
        assert token in logged_argv, f"missing {token!r} in argv: {logged_argv!r}"


@pytest.mark.parametrize("command", sorted(SHELL_OUT_EXPECTED))
def test_shell_out_missing_cli_reports_127(
    git_repo: Path, tmp_path: Path, command: str
) -> None:
    missing = tmp_path / "definitely-not-installed-xyz"
    proc = _run_lifecycle(git_repo, missing, command, "--json")
    assert proc.returncode != 0
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt["delegated_exit_code"] == 127
