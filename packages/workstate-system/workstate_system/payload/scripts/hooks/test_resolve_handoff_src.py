"""implementation note implementation note: in-tree-wins hook source resolution."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).with_name("resolve_handoff_src.py")
    spec = importlib.util.spec_from_file_location("resolve_handoff_src", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_source_repo_prefers_in_tree_over_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_module()
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
    (in_tree / "marker").write_text("in-tree")
    (overlay / "marker").write_text("overlay")

    from importlib import metadata as importlib_metadata

    def fake_distribution(_name: str):
        raise importlib_metadata.PackageNotFoundError(_name)

    monkeypatch.setattr(importlib_metadata, "distribution", fake_distribution)

    assert mod.resolve_agent_handoff_src(str(tmp_path)) == str(in_tree)


def test_consumer_fixture_prefers_overlay_over_in_tree_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_module()
    overlay = (
        tmp_path
        / ".workstate"
        / "remote"
        / "packages"
        / "mcp-workstate-handoff"
        / "src"
    )
    overlay.mkdir(parents=True)

    from importlib import metadata as importlib_metadata

    def fake_distribution(_name: str):
        raise importlib_metadata.PackageNotFoundError(_name)

    monkeypatch.setattr(importlib_metadata, "distribution", fake_distribution)

    assert mod.resolve_agent_handoff_src(str(tmp_path)) == str(overlay)
