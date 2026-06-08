"""implementation note implementation note regression guard, collected by ``make check-system``.

The payload's own test
(``workstate_system/payload/scripts/hooks/test_resolve_handoff_src.py``) lives
outside ``tests/`` and is therefore never collected by ``check-system`` / CI
(``testpaths = ["tests"]``). This shim re-exercises the in-tree-wins resolver
from a collected path so the slice keeps an automated guard.
"""

from __future__ import annotations

import importlib.util
from importlib import metadata as importlib_metadata
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RESOLVER_PATH = (
    PACKAGE_ROOT
    / "workstate_system"
    / "payload"
    / "scripts"
    / "hooks"
    / "resolve_handoff_src.py"
)


def _load_resolver():
    spec = importlib.util.spec_from_file_location(
        "payload_resolve_handoff_src", RESOLVER_PATH
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _force_no_installed_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_name: str):
        raise importlib_metadata.PackageNotFoundError(_name)

    monkeypatch.setattr(importlib_metadata, "distribution", _raise)


def test_resolver_file_is_shipped() -> None:
    assert RESOLVER_PATH.is_file()


def test_source_repo_prefers_in_tree_over_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_resolver()
    in_tree = tmp_path / "packages" / "mcp-workstate-handoff" / "src"
    overlay = (
        tmp_path
        / ".workstate"
        / "remote"
        / "packages"
        / "mcp-workstate-handoff"
        / "src"
    )
    in_tree.mkdir(parents=True)
    overlay.mkdir(parents=True)
    _force_no_installed_distribution(monkeypatch)
    assert mod.resolve_agent_handoff_src(str(tmp_path)) == str(in_tree)


def test_consumer_fixture_prefers_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_resolver()
    overlay = (
        tmp_path
        / ".workstate"
        / "remote"
        / "packages"
        / "mcp-workstate-handoff"
        / "src"
    )
    overlay.mkdir(parents=True)
    _force_no_installed_distribution(monkeypatch)
    assert mod.resolve_agent_handoff_src(str(tmp_path)) == str(overlay)
