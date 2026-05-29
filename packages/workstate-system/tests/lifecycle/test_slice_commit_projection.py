"""slice-commit must project the new HEAD into handoff_state (WORKSTATE-REF-SLICE-COMMIT-PROJECTION-20260509).

Today ``slice-commit`` creates a git commit but never projects the new
HEAD into ``handoff_state.updated_commit_sha``, so subsequent MCP writes
(notably ``update_review_finding``) see a stale workspace_commit_sha and
the commit_guard mistakenly rejects fixes that landed on a descendant.

The pin in this file is: after a successful ``git commit`` the handler
shells out to ``mcp-workstate-handoff set`` (the canonical actor-resolved
projector — it always rewrites ``updated_commit_sha`` from the current
worktree's git HEAD on any update, see
``packages/mcp-workstate-handoff/src/workstate_handoff_mcp/handoff_state.py``).

Tests follow the fake-CLI pattern from ``test_slice_start.py``: a fake
``mcp-workstate-handoff`` shim logs argv to a file so the test can verify
the projection call shape without booting a real handoff DB.
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
    tracked = repo / "tracked.txt"
    tracked.write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "-m", "init", "-q",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "-b", "feature/WORKSTATE-21"],
        check=True,
    )
    return repo


def _run_slice_commit(
    cwd: Path,
    fake_cli: Path | None,
    *extra: str,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if fake_cli is not None:
        env["MCP_AGENT_HANDOFF_BIN"] = str(fake_cli)
    else:
        env["MCP_AGENT_HANDOFF_BIN"] = "/nonexistent/no-such-binary-xyz"
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "slice-commit", *extra],
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


def _subcommand_of(block: list[str]) -> str | None:
    """Strip global flags (e.g. ``--workspace-root <path>``) and return
    the first positional, which is the CLI subcommand."""
    i = 0
    while i < len(block):
        tok = block[i]
        if tok.startswith("--"):
            # Global flags consume one value (no boolean-only flag is
            # used in our argv set here).
            i += 2
            continue
        return tok
    return None


# Fake CLI that, when invoked with the ``state`` (identity read)
# subcommand, prints a minimal identity envelope giving the row's
# ``revision``. Any other subcommand (e.g. ``set``) is logged and
# exits 0. This lets the projection do a read-then-write cycle
# deterministically without a real DB. After WORKSTATE-REF-51 implementation note the
# stored ``target_worktree_path`` no longer drives projection
# correctness — the projector passes ``--commit-sha`` / ``--branch``
# explicitly — so the identity payload omits it.
_FAKE_CLI_BODY = r"""#!/usr/bin/env bash
ARGV_LOG="__ARGV_LOG__"
printf "%s\n" "$@" >> "$ARGV_LOG"
echo "---" >> "$ARGV_LOG"
# Walk argv to find the subcommand (first non-flag positional).
sub=""
prev=""
for tok in "$@"; do
  case "$tok" in
    --*)
      prev="$tok"
      ;;
    *)
      if [[ "$prev" == --* && "$prev" != "--no-write" ]]; then
        prev=""
        continue
      fi
      sub="$tok"
      break
      ;;
  esac
done
if [[ "$sub" == "state" || "$sub" == "handoff-rows" ]]; then
  cat <<'JSON'
{"ok": true, "data": {"active": {"task_ref": "WORKSTATE-REF-21", "revision": 7, "target_branch": "feature/WORKSTATE-21"}}}
JSON
fi
exit 0
"""


def _make_fake_cli(
    fake_cli_dir: Path,
    argv_log: Path,
) -> Path:
    fake_cli_dir.mkdir(exist_ok=True)
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    body = _FAKE_CLI_BODY.replace("__ARGV_LOG__", str(argv_log))
    _write_fake_cli(fake_cli, body)
    return fake_cli


def test_slice_commit_projects_new_head_via_set(
    git_repo: Path, tmp_path: Path
) -> None:
    """RED gate for WORKSTATE-REF-SLICE-COMMIT-PROJECTION-20260509.

    After the commit lands, slice-commit must invoke
    ``mcp-workstate-handoff set`` with ``--task-ref`` and
    ``--expected-revision`` so the actor-resolved projector refreshes
    ``updated_commit_sha`` from the worktree HEAD. Today no such call
    happens — the receipt's ``handoff_projection`` stays ``"pending"``
    forever and the row drifts.
    """
    tracked = git_repo / "tracked.txt"
    tracked.write_text("seed\nchanged\n", encoding="utf-8")

    argv_log = tmp_path / "argv.log"
    fake_cli = _make_fake_cli(tmp_path / "fake-cli", argv_log)

    proc = _run_slice_commit(
        git_repo,
        fake_cli,
        "--msg", "feat(workstate-system): test slice commit",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True

    blocks = _split_argv_blocks(argv_log)
    set_blocks = [b for b in blocks if _subcommand_of(b) == "set"]
    assert set_blocks, (
        f"expected at least one `set` projection call, got argv blocks: "
        f"{blocks!r}"
    )
    args = set_blocks[-1]
    assert "--task-ref" in args
    assert args[args.index("--task-ref") + 1] == "WORKSTATE-REF-21"
    assert "--expected-revision" in args


def test_slice_commit_receipt_marks_projection_synced(
    git_repo: Path, tmp_path: Path
) -> None:
    """Companion assertion: the receipt's ``handoff_projection`` flips
    from ``"pending"`` to ``"synced"`` once the projection succeeds.
    The field has been ``"pending"``-by-construction since slice-commit
    shipped; this is the contract change a downstream auditor reads."""
    tracked = git_repo / "tracked.txt"
    tracked.write_text("seed\nchanged\n", encoding="utf-8")

    argv_log = tmp_path / "argv.log"
    fake_cli = _make_fake_cli(tmp_path / "fake-cli", argv_log)

    proc = _run_slice_commit(
        git_repo,
        fake_cli,
        "--msg", "feat(workstate-system): test slice commit",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["handoff_projection"] == "synced", (
        f"receipt should mark projection synced after fake-CLI returned ok; "
        f"receipt: {receipt!r}"
    )


def test_slice_commit_projection_argv_carries_explicit_commit_sha_and_branch(
    git_repo: Path, tmp_path: Path
) -> None:
    """WORKSTATE-REF-51 implementation note regression: the projection must pass the worktree's
    HEAD and branch via the new ``--commit-sha`` / ``--branch`` actor
    flags, in a single ``set`` call (no calibration round). This
    bypasses the resolver's stored-row task_git fallback so the
    projector writes the correct HEAD even when the row's
    ``target_worktree_path`` is null or points elsewhere — the source of
    the silent-stale-by-one-revision symptom under the calibrate-then-
    project pattern.
    """
    tracked = git_repo / "tracked.txt"
    tracked.write_text("seed\nchanged\n", encoding="utf-8")

    argv_log = tmp_path / "argv.log"
    fake_cli = _make_fake_cli(tmp_path / "fake-cli", argv_log)

    proc = _run_slice_commit(
        git_repo,
        fake_cli,
        "--msg", "feat(workstate-system): test slice commit",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    head_sha = receipt["commit_sha"]
    branch = receipt["branch"]

    blocks = _split_argv_blocks(argv_log)
    set_blocks = [b for b in blocks if _subcommand_of(b) == "set"]
    assert len(set_blocks) == 1, (
        f"expected exactly one `set` call (single explicit-commit projection); "
        f"argv blocks: {blocks!r}"
    )
    proj = set_blocks[0]
    assert "--commit-sha" in proj, proj
    assert proj[proj.index("--commit-sha") + 1] == head_sha, proj
    assert "--branch" in proj, proj
    assert proj[proj.index("--branch") + 1] == branch, proj
    # Calibration is no longer needed; the projection MUST NOT carry
    # --target-worktree-path (that channel is the old stored-row coercion).
    assert "--target-worktree-path" not in proj, proj


def _resolve_real_handoff_cli() -> Path:
    """Resolve the real ``mcp-workstate-handoff`` executable for the e2e pin.

    WORKSTATE-REF-51-BR-02 fix: ``shutil.which`` is the only resolution channel
    so the regression pin runs in CI (which installs the package
    editable and exposes the entry point on ``PATH``) and does not
    silently skip when a worktree-local ``.venv`` is absent. Raise
    ``RuntimeError`` when the CLI is not discoverable so the test job
    fails loudly instead of producing a green-with-skipped result that
    masks the wiring regression.
    """
    import shutil

    resolved = shutil.which("mcp-workstate-handoff")
    if resolved:
        return Path(resolved)
    raise RuntimeError(
        "mcp-workstate-handoff CLI not found on PATH; install the package "
        "(`pip install -e ./packages/mcp-workstate-handoff` or `uv sync "
        "--extra dev` in that package) before running the workstate-system "
        "lifecycle tests."
    )


def test_resolve_real_handoff_cli_uses_path(monkeypatch, tmp_path: Path) -> None:
    """Regression pin for WORKSTATE-REF-51-BR-02: the e2e CLI resolver MUST find
    ``mcp-workstate-handoff`` via ``PATH`` (not a worktree-local ``.venv``)
    and MUST raise rather than return a sentinel when the binary is
    absent. Skipping silently in CI hid the slice-commit projection
    regression once already; this pin makes the gate enforced.
    """
    import shutil as _shutil

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    shim = fake_bin / "mcp-workstate-handoff"
    shim.write_text("#!/bin/sh\nexit 0\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    assert _shutil.which("mcp-workstate-handoff") == str(shim)
    assert _resolve_real_handoff_cli() == shim

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert _shutil.which("mcp-workstate-handoff") is None
    with pytest.raises(RuntimeError, match="mcp-workstate-handoff"):
        _resolve_real_handoff_cli()


def test_slice_commit_drives_real_handoff_row_to_new_head(
    git_repo: Path, tmp_path: Path
) -> None:
    """End-to-end pin (WORKSTATE-REF-51-BR-01): slice-commit, shelling out to the
    real ``mcp-workstate-handoff`` CLI, must update the row's
    ``updated_commit_sha`` to the new HEAD and advance the revision by
    exactly one. Proves the wiring across lifecycle shell-out → CLI
    parsing → workspace-root selection → handoff write context end to
    end on a fresh WORKSTATE-REF fixture, not just argv shape against a fake
    shim. The fake-CLI suite above pins the call shape; this pin guards
    against a regression where the call shape stays correct but the
    actual write context flows the wrong commit_sha into the row.
    """
    real_cli = _resolve_real_handoff_cli()

    # State dir lives outside the git repo so slice-commit's
    # untracked-files guard doesn't trip on the handoff DB.
    state_dir = tmp_path / "handoff-state"
    state_dir.mkdir()
    handoff_env_overrides = {
        "AGENT_HANDOFF_WORKSPACE_ROOT": str(git_repo),
        "AGENT_HANDOFF_STATE_DIR": str(state_dir),
    }

    # Use the upper-cased form because slice-commit's handler normalizes
    # ``--task`` via ``.upper()``; handoff itself preserves case, so the
    # seed must already match the form slice-commit will look up.
    task_ref = "WORKSTATE-REF-TASK-51-E2E"

    seed_env = os.environ.copy()
    seed_env.update(handoff_env_overrides)
    seed = subprocess.run(
        [
            str(real_cli),
            "--workspace-root", str(git_repo),
            "--state-dir", str(state_dir),
            "set",
            "--task-ref", task_ref,
            "--objective", "WORKSTATE-REF-51 e2e regression pin",
            "--target-branch", "feature/WORKSTATE-21",
            "--target-worktree-path", str(git_repo),
        ],
        capture_output=True, text=True, check=False, env=seed_env,
    )
    assert seed.returncode == 0, (seed.stdout, seed.stderr)
    seed_row = json.loads(seed.stdout)["data"]["active"]
    seed_revision = int(seed_row["revision"])
    seed_commit_sha = seed_row["updated_commit_sha"]

    # Edit + slice-commit using the real handoff CLI for the projection.
    tracked = git_repo / "tracked.txt"
    tracked.write_text("seed\nchanged\n", encoding="utf-8")

    proc = _run_slice_commit(
        git_repo,
        real_cli,
        "--task", task_ref,
        "--msg", "feat(test): WORKSTATE-REF-51 e2e projection pin",
        "--json",
        env_overrides=handoff_env_overrides,
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["handoff_projection"] == "synced", json.dumps(receipt, indent=2)
    new_head = receipt["commit_sha"]
    assert new_head and new_head != seed_commit_sha

    # Read back the row through the real CLI and verify the projection.
    state = subprocess.run(
        [
            str(real_cli),
            "--workspace-root", str(git_repo),
            "--state-dir", str(state_dir),
            "state",
            "--sections", "identity",
            task_ref,
        ],
        capture_output=True, text=True, check=False, env=seed_env,
    )
    assert state.returncode == 0, (state.stdout, state.stderr)
    active = json.loads(state.stdout)["data"]["active"]
    assert active["updated_commit_sha"] == new_head, active
    assert int(active["revision"]) == seed_revision + 1, active


def test_slice_commit_offline_marks_projection_pending(
    git_repo: Path, tmp_path: Path
) -> None:
    """Best-effort guarantee: when the projection CLI is unreachable
    (binary missing), slice-commit must still succeed (commit landed)
    but the receipt carries ``handoff_projection == "pending"`` and a
    ``projection_warning`` field — never failing the slice-commit call.
    """
    tracked = git_repo / "tracked.txt"
    tracked.write_text("seed\nchanged\n", encoding="utf-8")

    proc = _run_slice_commit(
        git_repo,
        None,  # forces MCP_AGENT_HANDOFF_BIN to a nonexistent path
        "--msg", "feat(workstate-system): test slice commit",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True, receipt
    assert receipt["handoff_projection"] == "pending", receipt
    assert receipt.get("projection_warning"), (
        f"expected projection_warning when CLI is missing; receipt={receipt!r}"
    )
    # Commit still landed.
    assert _git(git_repo, "rev-parse", "HEAD") == receipt["commit_sha"]
