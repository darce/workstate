"""implementation note implementation note: ``make ensure-hooks-path`` rewires the source repo's
``core.hooksPath`` to ``scripts/hooks/git`` (idempotently).

The target is the protocol monorepo's source-repo workflow hook — when a
maintainer's ``.git/config`` still carries the legacy
``core.hooksPath = scripts/hooks`` value (or any other broken value),
running ``make ensure-hooks-path`` from the monorepo root must rewire it
without an embedded operator step.

Skipped when the sibling monorepo Makefile is not on disk (so the
workstate-bootstrap package can be tested in isolation).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _monorepo_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[3]
    return candidate if (candidate / "Makefile").is_file() else None


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


@pytest.mark.parametrize(
    "broken_value",
    [
        "scripts/hooks",  # the legacy / broken parent value
        "scripts/wrong-path",
        "",  # unset — exercise the no-current-value branch
    ],
)
def test_ensure_hooks_path_rewires_broken_value(
    tmp_path: Path, broken_value: str
) -> None:
    monorepo = _monorepo_root()
    if monorepo is None:
        pytest.skip("monorepo root Makefile not available in this environment")

    target = tmp_path / "consumer"
    target.mkdir()
    _git("init", "--initial-branch=main", cwd=target)
    _git("config", "user.email", "test@example.com", cwd=target)
    _git("config", "user.name", "Test", cwd=target)
    if broken_value:
        _git("config", "core.hooksPath", broken_value, cwd=target)

    makefile = monorepo / "Makefile"
    shutil.copy2(makefile, target / "Makefile")

    result = subprocess.run(
        ["make", "ensure-hooks-path"],
        cwd=target,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    final = _git("config", "--get", "core.hooksPath", cwd=target)
    assert final == "scripts/hooks/git"


def test_ensure_hooks_path_is_idempotent(tmp_path: Path) -> None:
    monorepo = _monorepo_root()
    if monorepo is None:
        pytest.skip("monorepo root Makefile not available in this environment")

    target = tmp_path / "consumer"
    target.mkdir()
    _git("init", "--initial-branch=main", cwd=target)
    _git("config", "user.email", "test@example.com", cwd=target)
    _git("config", "user.name", "Test", cwd=target)
    _git("config", "core.hooksPath", "scripts/hooks/git", cwd=target)

    shutil.copy2(monorepo / "Makefile", target / "Makefile")

    first = subprocess.run(
        ["make", "ensure-hooks-path"], cwd=target, capture_output=True, text=True
    )
    second = subprocess.run(
        ["make", "ensure-hooks-path"], cwd=target, capture_output=True, text=True
    )

    assert first.returncode == 0
    assert second.returncode == 0
    # When the value is already correct the recipe prints nothing on the
    # rewrite line.
    assert "core.hooksPath:" not in second.stdout
    assert (
        _git("config", "--get", "core.hooksPath", cwd=target) == "scripts/hooks/git"
    )
