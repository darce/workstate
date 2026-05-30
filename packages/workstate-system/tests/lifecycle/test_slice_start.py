"""implementation note contract tests for the mutating ``slice-start`` subcommand.

The handler is the RED-evidence measurement point: it derives the
task ref from the current git context, runs (or dry-runs) the supplied
``TEST_CMD``, records the result with the current HEAD, and emits a
stable JSON receipt.

Receipt extras (per ``§JSON Receipt Schema`` in implementation note):

* ``slice_slug``: ``str | None`` — optional caller-supplied label
* ``test_command``: ``str`` — the command that was run
* ``test_passed``: ``bool`` — whether the command exited 0
* ``red_evidence_sha``: ``str`` — HEAD at which the result was recorded
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
PENDING_REL = Path(".task-state") / "pending-workflow-events.jsonl"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _write_fake_cli(target: Path, body: str) -> None:
    target.write_text(body)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


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
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "-b", "feature/WORKSTATE-21"],
        check=True,
    )
    return repo


@pytest.fixture
def fake_cli_dir(tmp_path: Path) -> Path:
    return tmp_path / "fake-cli"


def _run_slice_start(
    cwd: Path,
    fake_cli: Path | None,
    *extra: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if fake_cli is not None:
        env["MCP_WORKSTATE_HANDOFF_BIN"] = str(fake_cli)
    else:
        env["MCP_WORKSTATE_HANDOFF_BIN"] = "/nonexistent/no-such-binary-xyz"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "slice-start", *extra],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _split_argv_blocks(log_path: Path) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    if not log_path.exists():
        return blocks
    for line in log_path.read_text().splitlines():
        if line == "---":
            blocks.append(current)
            current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


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

SLICE_START_EXTRA_FIELDS = (
    "slice_slug",
    "test_command",
    "test_passed",
    "red_evidence_sha",
)


def test_slice_start_passing_test_records_green_evidence(
    git_repo: Path, fake_cli_dir: Path, tmp_path: Path
) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    argv_log = tmp_path / "argv.log"
    _write_fake_cli(
        fake_cli,
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> {argv_log}\n'
        f'echo "---" >> {argv_log}\nexit 0\n',
    )
    proc = _run_slice_start(
        git_repo,
        fake_cli,
        "--test-cmd", "true",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    for field in (*REQUIRED_FIELDS, *SLICE_START_EXTRA_FIELDS):
        assert field in receipt, f"missing field {field!r}: {receipt!r}"
    assert receipt["ok"] is True
    assert receipt["command"] == "slice-start"
    assert receipt["task_ref"] == "WORKSTATE-REF-21"
    assert receipt["branch"] == "feature/WORKSTATE-21"
    assert receipt["test_command"] == "true"
    assert receipt["test_passed"] is True
    assert receipt["handoff_projection"] == "synced"
    assert "slice_started" in receipt["events"]
    assert "test_recorded" in receipt["events"]
    assert len(receipt["red_evidence_sha"]) == 40
    # Fake CLI received a test_result event tied to the HEAD sha.
    blocks = _split_argv_blocks(argv_log)
    event_blocks = [
        b for b in blocks
        if "event" in b and "test_result" in b
    ]
    assert event_blocks, f"no test_result event recorded: {blocks!r}"
    args = event_blocks[-1]
    assert "--passed" in args
    assert "--command" in args
    assert args[args.index("--command") + 1] == "true"


def test_slice_start_dependency_missing_records_test_setup_debt(
    git_repo: Path, fake_cli_dir: Path, tmp_path: Path
) -> None:
    """implementation note.3: when TEST_CMD's executable cannot be located (POSIX
    shell exit 127), slice-start must distinguish the failure from RED
    evidence: the receipt carries ``test_setup_debt: true`` and the
    events list includes ``"test_setup_debt"`` instead of the regular
    ``"test_recorded"`` entry. RED-evidence signal (``test_passed``)
    stays false because the command did not pass, but consumers can
    branch on ``test_setup_debt`` to skip RED-gate enforcement.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    argv_log = tmp_path / "argv.log"
    _write_fake_cli(
        fake_cli,
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> {argv_log}\n'
        f'echo "---" >> {argv_log}\nexit 0\n',
    )
    proc = _run_slice_start(
        git_repo,
        fake_cli,
        "--test-cmd", "nonexistent-binary-xyz-9999",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["test_passed"] is False
    assert receipt.get("test_setup_debt") is True, receipt
    assert "test_setup_debt" in receipt["events"], receipt["events"]
    assert "test_recorded" not in receipt["events"], receipt["events"]


def test_slice_start_failing_test_records_red_evidence(
    git_repo: Path, fake_cli_dir: Path, tmp_path: Path
) -> None:
    """Failing TEST_CMD must NOT block the receipt: slice-start
    completes with ``test_passed=false`` and exit 0 because RED evidence
    is the whole point of the first run.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    argv_log = tmp_path / "argv.log"
    _write_fake_cli(
        fake_cli,
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> {argv_log}\n'
        f'echo "---" >> {argv_log}\nexit 0\n',
    )
    proc = _run_slice_start(
        git_repo,
        fake_cli,
        "--test-cmd", "false",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["test_passed"] is False
    assert receipt["test_command"] == "false"
    assert "test_recorded" in receipt["events"]
    blocks = _split_argv_blocks(argv_log)
    event_blocks = [
        b for b in blocks
        if "event" in b and "test_result" in b
    ]
    assert event_blocks, blocks
    args = event_blocks[-1]
    # Failing test must NOT carry --passed (argparse store_true).
    assert "--passed" not in args
    # exit code surfaced
    assert "--exit-code" in args
    assert args[args.index("--exit-code") + 1] == "1"


def test_slice_start_passing_test_records_green_evidence_phase(
    git_repo: Path, fake_cli_dir: Path, tmp_path: Path
) -> None:
    """implementation note.4: a passing TEST_CMD records GREEN evidence — receipt
    carries ``evidence_phase == "green"`` and ``green_evidence_sha`` is
    populated with the current HEAD. ``red_evidence_sha`` stays
    populated for back-compat (it has always been "the HEAD that
    produced the recorded evidence" regardless of phase), but consumers
    that need RED-vs-GREEN distinction read ``evidence_phase``.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    argv_log = tmp_path / "argv.log"
    _write_fake_cli(
        fake_cli,
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> {argv_log}\n'
        f'echo "---" >> {argv_log}\nexit 0\n',
    )
    proc = _run_slice_start(
        git_repo,
        fake_cli,
        "--test-cmd", "true",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["test_passed"] is True
    assert receipt.get("evidence_phase") == "green", receipt
    assert len(receipt.get("green_evidence_sha", "")) == 40, receipt
    assert receipt["green_evidence_sha"] == receipt["head"]


def test_slice_start_failing_test_records_red_evidence_phase(
    git_repo: Path, fake_cli_dir: Path, tmp_path: Path
) -> None:
    """implementation note.4: a failing TEST_CMD records RED evidence — receipt
    carries ``evidence_phase == "red"`` and ``green_evidence_sha`` is
    empty. ``red_evidence_sha`` carries the HEAD that produced the
    failure as before.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    argv_log = tmp_path / "argv.log"
    _write_fake_cli(
        fake_cli,
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> {argv_log}\n'
        f'echo "---" >> {argv_log}\nexit 0\n',
    )
    proc = _run_slice_start(
        git_repo,
        fake_cli,
        "--test-cmd", "false",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["test_passed"] is False
    assert receipt.get("evidence_phase") == "red", receipt
    assert receipt.get("green_evidence_sha", "") == "", receipt
    assert len(receipt["red_evidence_sha"]) == 40


def test_slice_start_setup_debt_records_no_evidence_phase(
    git_repo: Path, fake_cli_dir: Path, tmp_path: Path
) -> None:
    """implementation note.4: when ``test_setup_debt`` fires (shell-127), no real
    measurement happened. The receipt's ``evidence_phase`` must be
    ``None`` so consumers do not classify the run as RED or GREEN.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    argv_log = tmp_path / "argv.log"
    _write_fake_cli(
        fake_cli,
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> {argv_log}\n'
        f'echo "---" >> {argv_log}\nexit 0\n',
    )
    proc = _run_slice_start(
        git_repo,
        fake_cli,
        "--test-cmd", "nonexistent-binary-xyz-9999",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["test_setup_debt"] is True
    assert receipt.get("evidence_phase") is None, receipt
    assert receipt.get("green_evidence_sha", "") == "", receipt


def test_slice_start_runs_test_cmd_with_root_venv_pytest_over_pyenv(
    git_repo: Path, fake_cli_dir: Path, tmp_path: Path
) -> None:
    """WORKSTATE-REF-07 implementation note: ``slice-start`` refreshes root ``.venv`` provisioning
    and executes ``TEST_CMD`` with ``<worktree>/.venv/bin`` *before* the
    ambient PATH. A pyenv-like ``pytest`` shim placed earlier in the
    original PATH must lose to the worktree-local one — proving bare
    ``pytest`` resolves locally instead of leaking the primary checkout.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")

    # A discoverable package so root provisioning actually creates the venv.
    pkg = git_repo / "packages" / "alpha"
    pkg.mkdir(parents=True)
    (pkg / "pyproject.toml").write_text("[project]\nname='x'\n")

    marker = tmp_path / "which_pytest.txt"

    # fake ``uv``: on ``venv`` materialize both python and a ``pytest`` that
    # writes ROOT to the marker; ``pip install`` / ``sync`` are no-ops.
    fake_uv = tmp_path / "fake-uv"
    _write_fake_cli(
        fake_uv,
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "--version" ]]; then echo "uv 0.4.0"; exit 0; fi\n'
        'if [[ "$1" == "venv" ]]; then\n'
        '  mkdir -p "$2/bin"\n'
        '  : > "$2/bin/python"; chmod +x "$2/bin/python"\n'
        f"  printf '#!/usr/bin/env bash\\necho ROOT > {marker}\\nexit 0\\n' "
        '> "$2/bin/pytest"\n'
        '  chmod +x "$2/bin/pytest"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )

    # A pyenv-like pytest earlier in the *original* PATH that, if it won,
    # would write PYENV instead.
    pyenv_bin = tmp_path / "pyenv-bin"
    pyenv_bin.mkdir()
    _write_fake_cli(
        pyenv_bin / "pytest",
        f"#!/usr/bin/env bash\necho PYENV > {marker}\nexit 0\n",
    )

    proc = _run_slice_start(
        git_repo,
        fake_cli,
        "--test-cmd", "pytest",
        "--json",
        extra_env={
            "WORKSTATE_LIFECYCLE_UV_BIN": str(fake_uv),
            "PATH": str(pyenv_bin) + os.pathsep + os.environ["PATH"],
        },
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["test_passed"] is True, receipt
    assert marker.exists(), "TEST_CMD pytest never ran"
    assert marker.read_text().strip() == "ROOT", (
        "TEST_CMD pytest resolved via the pyenv shim instead of the "
        "worktree-local root .venv"
    )
