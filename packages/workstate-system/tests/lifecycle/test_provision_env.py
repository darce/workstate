"""WORKSTATE-REF-07 implementation note: the reusable ``provision-env`` lifecycle subcommand.

``provision-env --worktree <path>`` provisions a worktree-root ``.venv``
for any worktree (orchestrator fresh lanes, the ``worktree-lane`` shell
asset) and emits a compact, stable JSON receipt that those callers can
inspect.
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

# fake ``uv`` that materializes ``<venv>/bin/python`` on ``venv`` so the
# WORKSTATE-REF-07 helper's ``python_path.exists()`` validation passes; ``pip``/``sync``
# are no-ops.
_FAKE_UV_BODY = (
    "#!/usr/bin/env bash\n"
    'if [[ "$1" == "--version" ]]; then echo "uv 0.4.0"; exit 0; fi\n'
    'if [[ "$1" == "venv" ]]; then mkdir -p "$2/bin"; : > "$2/bin/python"; '
    'chmod +x "$2/bin/python"; fi\n'
    "exit 0\n"
)


def _write_executable(target: Path, body: str) -> None:
    target.write_text(body)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _seed_pkg(root: Path, name: str) -> Path:
    pkg = root / "packages" / name
    pkg.mkdir(parents=True)
    (pkg / "pyproject.toml").write_text("[project]\nname='x'\n")
    return pkg


def _run_provision_env(
    *,
    worktree: str,
    uv_bin: str | Path | None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if uv_bin is None:
        env.pop("WORKSTATE_LIFECYCLE_UV_BIN", None)
    else:
        env["WORKSTATE_LIFECYCLE_UV_BIN"] = str(uv_bin)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            sys.executable,
            str(LIFECYCLE_PKG),
            "provision-env",
            "--worktree",
            worktree,
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_provision_env_creates_root_venv_and_emits_receipt(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    _seed_pkg(worktree, "alpha")

    fake_uv = tmp_path / "fake-uv"
    _write_executable(fake_uv, _FAKE_UV_BODY)

    proc = _run_provision_env(worktree=str(worktree), uv_bin=fake_uv)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["command"] == "provision-env"
    assert receipt["worktree_path"] == str(worktree)
    assert receipt["created"] is True
    assert receipt["root_venv_path"] == str(worktree / ".venv")
    assert receipt["python_path"] == str(worktree / ".venv" / "bin" / "python")
    assert receipt["installed"] == ["alpha"]
    assert receipt["skipped"] == []
    assert (worktree / ".venv" / "bin" / "python").exists()


def test_provision_env_reprovisions_existing_venv_with_clear(tmp_path: Path) -> None:
    """WORKSTATE-REF-07-followups review fix: provision-env recovers an existing .venv.

    The manual recovery path (and the command the doctor venv facet points
    operators at) must pass ``uv venv --clear`` so a pre-existing — possibly
    partial — ``.venv`` is replaced rather than aborting. This fake ``uv``
    rejects ``venv`` unless ``--clear`` is present, mirroring real uv's
    "A virtual environment already exists" abort, so the test fails if the
    handler ever drops the flag.
    """
    worktree = tmp_path / "wt"
    _seed_pkg(worktree, "alpha")
    (worktree / ".venv").mkdir(parents=True)

    fake_uv = tmp_path / "fake-uv"
    _write_executable(
        fake_uv,
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "--version" ]]; then echo "uv 0.4.0"; exit 0; fi\n'
        'if [[ "$1" == "venv" ]]; then\n'
        '  if [[ "$*" != *"--clear"* ]]; then echo "already exists" 1>&2; exit 1; fi\n'
        '  mkdir -p "$2/bin"; : > "$2/bin/python"; chmod +x "$2/bin/python"\n'
        "fi\n"
        "exit 0\n",
    )

    proc = _run_provision_env(worktree=str(worktree), uv_bin=fake_uv)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["created"] is True
    assert receipt["root_venv_path"] == str(worktree / ".venv")


def test_provision_env_no_packages_is_noop_success(tmp_path: Path) -> None:
    worktree = tmp_path / "empty-wt"
    worktree.mkdir()

    fake_uv = tmp_path / "fake-uv"
    _write_executable(fake_uv, _FAKE_UV_BODY)

    proc = _run_provision_env(worktree=str(worktree), uv_bin=fake_uv)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["created"] is False
    assert receipt["root_venv_path"] is None
    assert not (worktree / ".venv").exists()


def test_provision_env_missing_worktree_errors(tmp_path: Path) -> None:
    fake_uv = tmp_path / "fake-uv"
    _write_executable(fake_uv, _FAKE_UV_BODY)

    proc = _run_provision_env(
        worktree=str(tmp_path / "does-not-exist"), uv_bin=fake_uv
    )
    assert proc.returncode == 2
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt["error"] == "worktree_not_found"


def test_provision_env_venv_failure_is_hard_error(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    _seed_pkg(worktree, "alpha")

    # ``uv venv`` fails → hard provisioning failure (ok False, exit 2).
    fake_uv = tmp_path / "fake-uv"
    _write_executable(
        fake_uv,
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "--version" ]]; then echo "uv 0.4.0"; exit 0; fi\n'
        'if [[ "$1" == "venv" ]]; then echo "boom" 1>&2; exit 1; fi\n'
        "exit 0\n",
    )

    proc = _run_provision_env(worktree=str(worktree), uv_bin=fake_uv)
    assert proc.returncode == 2
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt["created"] is False
    assert "venv creation failed" in receipt["failure_reason"]
