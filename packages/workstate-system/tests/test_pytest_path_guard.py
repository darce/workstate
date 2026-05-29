"""WORKSTATE-REF-41 implementation note: shared per-package pytest path-guard helper.

The guard walks already-imported in-repo agentic packages and raises
``pytest.UsageError`` if any resolves outside the active worktree root.
``AGENTIC_DISABLE_PYTEST_PATH_GUARD=1`` opts out.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pytest_path_guard  # noqa: WORKSTATE-REF-402


def _fake_module(name: str, file_path: Path) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__file__ = str(file_path)
    return mod


def test_collect_violations_returns_empty_when_all_modules_inside_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    inside = worktree / "packages" / "x" / "src" / "workstate_handoff_mcp" / "__init__.py"
    inside.parent.mkdir(parents=True)
    inside.write_text("")

    modules = [("workstate_handoff_mcp", _fake_module("workstate_handoff_mcp", inside))]
    assert pytest_path_guard.collect_violations(worktree, modules=modules) == []


def test_collect_violations_flags_modules_outside_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    elsewhere = tmp_path / "other-worktree" / "src" / "workstate_handoff_mcp" / "__init__.py"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("")

    modules = [("workstate_handoff_mcp", _fake_module("workstate_handoff_mcp", elsewhere))]
    violations = pytest_path_guard.collect_violations(worktree, modules=modules)
    assert len(violations) == 1
    name, actual, root = violations[0]
    assert name == "workstate_handoff_mcp"
    assert actual == elsewhere.resolve()
    assert root == worktree.resolve()


def test_collect_violations_includes_workstate_prefixed_packages(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    elsewhere = tmp_path / "other" / "workstate_protocol" / "__init__.py"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("")

    modules = [("workstate_protocol", _fake_module("workstate_protocol", elsewhere))]
    assert len(pytest_path_guard.collect_violations(worktree, modules=modules)) == 1


def test_collect_violations_skips_submodules(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    elsewhere = tmp_path / "other" / "workstate_handoff_mcp" / "api.py"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("")

    modules = [("workstate_handoff_mcp.api", _fake_module("workstate_handoff_mcp.api", elsewhere))]
    assert pytest_path_guard.collect_violations(worktree, modules=modules) == []


def test_collect_violations_skips_unguarded_packages(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    elsewhere = tmp_path / "other" / "json_ext" / "__init__.py"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("")

    modules = [("json_ext", _fake_module("json_ext", elsewhere))]
    assert pytest_path_guard.collect_violations(worktree, modules=modules) == []


def test_collect_violations_ignores_modules_without_file(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    mod = types.ModuleType("workstate_handoff_mcp")  # no __file__
    modules = [("workstate_handoff_mcp", mod)]
    assert pytest_path_guard.collect_violations(worktree, modules=modules) == []


def test_remediation_message_names_path_and_cwd(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    elsewhere = tmp_path / "other" / "src" / "workstate_handoff_mcp" / "__init__.py"
    msg = pytest_path_guard.remediation_message(
        [("workstate_handoff_mcp", elsewhere, worktree)],
        cwd=tmp_path / "shell-cwd",
    )
    assert "workstate_handoff_mcp" in msg
    assert str(elsewhere) in msg
    assert "uv sync --extra dev" in msg
    assert str((tmp_path / "shell-cwd").resolve()) in msg


def test_check_path_guard_raises_on_violation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    elsewhere = tmp_path / "other" / "workstate_handoff_mcp" / "__init__.py"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("")
    fake = _fake_module("workstate_handoff_mcp", elsewhere)
    monkeypatch.setitem(sys.modules, "workstate_handoff_mcp", fake)
    monkeypatch.delenv(pytest_path_guard.OPT_OUT_ENV, raising=False)

    with pytest.raises(pytest.UsageError) as exc_info:
        pytest_path_guard.check_path_guard(worktree)
    assert "uv sync --extra dev" in str(exc_info.value)


def test_check_path_guard_opt_out_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    elsewhere = tmp_path / "other" / "workstate_handoff_mcp" / "__init__.py"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("")
    fake = _fake_module("workstate_handoff_mcp", elsewhere)
    monkeypatch.setitem(sys.modules, "workstate_handoff_mcp", fake)
    monkeypatch.setenv(pytest_path_guard.OPT_OUT_ENV, "1")

    pytest_path_guard.check_path_guard(worktree)  # must NOT raise


def test_check_path_guard_clean_session_does_not_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = tmp_path / "worktree"
    inside = worktree / "src" / "workstate_handoff_mcp" / "__init__.py"
    inside.parent.mkdir(parents=True)
    inside.write_text("")

    # Replace sys.modules with a snapshot containing only the one fake
    # in-worktree module so any real guarded modules already imported by
    # pytest startup do not pollute the assertion.
    fake = _fake_module("workstate_handoff_mcp", inside)
    pruned = {
        name: mod
        for name, mod in sys.modules.items()
        if not pytest_path_guard._is_guarded_top_level(name)
    }
    pruned["workstate_handoff_mcp"] = fake
    monkeypatch.setattr(sys, "modules", pruned)
    monkeypatch.delenv(pytest_path_guard.OPT_OUT_ENV, raising=False)

    pytest_path_guard.check_path_guard(worktree)
