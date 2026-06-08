"""WORKSTATE-REF-09: Grok plugin activation dispatcher tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_install import _init_git_repo, fake_remote_with_generator  # noqa: F401


def test_activate_grok_plugin_applied_when_cli_succeeds(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.activation import activate_grok_plugin
    from workstate_bootstrap.install import GROK_PLUGIN_DEST, install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    calls: list[tuple[str, ...]] = []

    def fake_run(args, *, cwd):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(["grok", "plugin", *args], 0, stdout="ok", stderr="")

    with patch("workstate_bootstrap.activation._grok_cli_available", return_value=True):
        with patch("workstate_bootstrap.activation._run_grok_cli", side_effect=fake_run):
            entry = activate_grok_plugin(target)

    assert entry["kind"] == "grok_plugin_activation"
    assert entry["action"] == "applied"
    assert entry["path"] == GROK_PLUGIN_DEST.as_posix()
    # Pin the exact CLI argv contract: install <dest> --trust, then enable
    # with the bare-name selector (never a project/<hash>/<name> form).
    plugin_dest = target / GROK_PLUGIN_DEST
    assert calls == [
        ("install", plugin_dest.as_posix(), "--trust"),
        ("enable", "workstate-system"),
    ]


def test_activate_grok_plugin_failed_when_enable_errors(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.activation import activate_grok_plugin
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    def fail_enable(args, *, cwd):
        if args[0] == "enable":
            return subprocess.CompletedProcess(["grok"], 1, stdout="", stderr="enable boom")
        return subprocess.CompletedProcess(["grok"], 0, stdout="ok", stderr="")

    with patch("workstate_bootstrap.activation._grok_cli_available", return_value=True):
        with patch("workstate_bootstrap.activation._run_grok_cli", side_effect=fail_enable):
            entry = activate_grok_plugin(target)

    assert entry["action"] == "failed"
    assert "enable boom" in entry.get("message", "")


@pytest.mark.parametrize("timeout_step", ["install", "enable"])
def test_activate_grok_plugin_failed_on_cli_timeout(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    timeout_step: str,
) -> None:
    from workstate_bootstrap.activation import activate_grok_plugin
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    def maybe_timeout(args, *, cwd):
        if args[0] == timeout_step:
            raise subprocess.TimeoutExpired(cmd=["grok", "plugin", *args], timeout=120)
        return subprocess.CompletedProcess(["grok"], 0, stdout="ok", stderr="")

    with patch("workstate_bootstrap.activation._grok_cli_available", return_value=True):
        with patch("workstate_bootstrap.activation._run_grok_cli", side_effect=maybe_timeout):
            entry = activate_grok_plugin(target)

    assert entry["action"] == "failed"
    assert f"grok plugin {timeout_step} timed out" in entry.get("message", "")


def test_activate_grok_plugin_failed_when_plugin_dest_missing(tmp_path: Path) -> None:
    from workstate_bootstrap.activation import activate_grok_plugin

    target = tmp_path / "consumer"
    target.mkdir()

    entry = activate_grok_plugin(target)

    assert entry["action"] == "failed"
    assert "missing" in entry.get("message", "")


def test_activate_grok_plugin_skipped_when_cli_missing(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.activation import activate_grok_plugin
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    with patch("workstate_bootstrap.activation._grok_cli_available", return_value=False):
        entry = activate_grok_plugin(target)

    assert entry["action"] == "skipped_no_cli"


def test_activate_grok_plugin_failed_when_install_errors(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.activation import activate_grok_plugin
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    def fail_install(args, *, cwd):
        return subprocess.CompletedProcess(["grok"], 1, stdout="", stderr="boom")

    with patch("workstate_bootstrap.activation._grok_cli_available", return_value=True):
        with patch("workstate_bootstrap.activation._run_grok_cli", side_effect=fail_install):
            entry = activate_grok_plugin(target)

    assert entry["action"] == "failed"
    assert "boom" in entry.get("message", "")


def test_activate_grok_plugin_idempotent_when_already_installed(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """Re-running activation when grok reports the plugin already installed is
    idempotent success: install's non-zero "already installed" is tolerated, the
    dispatcher still enables, and reports ``already_present`` (repeatable deploy)."""
    from workstate_bootstrap.activation import activate_grok_plugin
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    calls: list[tuple[str, ...]] = []

    def already_installed(args, *, cwd):
        calls.append(tuple(args))
        if args[0] == "install":
            return subprocess.CompletedProcess(
                ["grok"],
                1,
                stdout="",
                stderr="Error: Failed to install plugin: repo "
                "'workstate-system-6d3169fa' already installed",
            )
        return subprocess.CompletedProcess(["grok"], 0, stdout="ok", stderr="")

    with patch("workstate_bootstrap.activation._grok_cli_available", return_value=True):
        with patch(
            "workstate_bootstrap.activation._run_grok_cli",
            side_effect=already_installed,
        ):
            entry = activate_grok_plugin(target)

    # Distinct from a fresh "applied" so status/manifest readers can tell a
    # re-run apart from a first install.
    assert entry["action"] == "already_present"
    # Still proceeds to enable after tolerating the already-installed install.
    assert any(call[0] == "enable" for call in calls)


def test_activate_grok_plugin_failed_when_enable_errors_after_already_installed(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """Tolerating an already-installed install must NOT mask a genuinely broken
    activation: if `enable` then fails, the dispatcher still reports ``failed``."""
    from workstate_bootstrap.activation import activate_grok_plugin
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    def already_installed_then_enable_fails(args, *, cwd):
        if args[0] == "install":
            return subprocess.CompletedProcess(
                ["grok"], 1, stdout="", stderr="repo 'x' already installed"
            )
        return subprocess.CompletedProcess(["grok"], 1, stdout="", stderr="enable boom")

    with patch("workstate_bootstrap.activation._grok_cli_available", return_value=True):
        with patch(
            "workstate_bootstrap.activation._run_grok_cli",
            side_effect=already_installed_then_enable_fails,
        ):
            entry = activate_grok_plugin(target)

    assert entry["action"] == "failed"
    assert "enable boom" in entry.get("message", "")


def test_install_records_grok_plugin_activation_manifest_entry(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator

    with patch("workstate_bootstrap.activation._grok_cli_available", return_value=False):
        manifest = install(target=target, remote_url=url, remote_ref=ref)

    activation_entries = [
        entry
        for entry in manifest["configs"]
        if entry.get("kind") == "grok_plugin_activation"
    ]
    assert len(activation_entries) == 1
    assert activation_entries[0]["action"] == "skipped_no_cli"


def test_install_idempotent_rerun_re_records_grok_activation(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator

    with patch("workstate_bootstrap.activation._grok_cli_available", return_value=False):
        install(target=target, remote_url=url, remote_ref=ref)
        second = install(target=target, remote_url=url, remote_ref=ref)

    activation_entries = [
        entry
        for entry in second["configs"]
        if entry.get("kind") == "grok_plugin_activation"
    ]
    assert activation_entries
    assert activation_entries[0]["action"] in {"skipped_no_cli", "applied", "failed"}


def test_install_records_claude_and_codex_activation_receipts(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """install() records applied claude/codex activation receipts (WORKSTATE09-R2-B-004)."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator

    with patch("workstate_bootstrap.activation._grok_cli_available", return_value=False):
        manifest = install(target=target, remote_url=url, remote_ref=ref)

    by_kind = {
        entry["kind"]: entry
        for entry in manifest["configs"]
        if entry.get("kind", "").endswith("_plugin_activation")
    }
    assert by_kind["claude_plugin_activation"]["action"] == "applied"
    assert by_kind["codex_plugin_activation"]["action"] == "applied"
    assert by_kind["grok_plugin_activation"]["action"] == "skipped_no_cli"


def test_write_plugin_activation_skipped_no_contract(tmp_path: Path) -> None:
    """A clone without plugin_activation capability rows yields skipped_no_contract."""
    from workstate_bootstrap.activation import write_plugin_activation

    clone = tmp_path / "clone"
    clone.mkdir()
    target = tmp_path / "consumer"
    target.mkdir()

    for harness, kind in (
        ("claude-code", "claude_plugin_activation"),
        ("codex", "codex_plugin_activation"),
        ("grok", "grok_plugin_activation"),
    ):
        entry = write_plugin_activation(harness, target, clone=clone)
        assert entry["action"] == "skipped_no_contract"
        assert entry["kind"] == kind


def test_grok_bare_selector_enabled_missing_config_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing ~/.grok/config.toml means 'grok not in use', not 'disabled'
    (WORKSTATE09-R2-B-002): doctor must not emit activation drift for it."""
    from workstate_bootstrap.activation import grok_bare_selector_enabled

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    assert grok_bare_selector_enabled() is None
    assert grok_bare_selector_enabled(home) is None


def test_detect_stale_grok_discovery_selectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workstate_bootstrap.activation import detect_stale_grok_discovery_selectors

    home = tmp_path / "home"
    config = home / ".grok" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[plugins]\nenabled = ["project/a018ff86/workstate-system", "workstate-system"]\n'
    )
    monkeypatch.setenv("HOME", str(home))
    stale = detect_stale_grok_discovery_selectors(home)
    assert stale == ["project/a018ff86/workstate-system"]


def test_materialize_grok_plugin_symlink_points_at_effective_tree(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.activation import materialize_grok_plugin_symlink
    from workstate_bootstrap.install import GROK_PLUGIN_DEST, install

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    import shutil

    if (target / GROK_PLUGIN_DEST).exists():
        if (target / GROK_PLUGIN_DEST).is_symlink():
            (target / GROK_PLUGIN_DEST).unlink()
        else:
            shutil.rmtree(target / GROK_PLUGIN_DEST)

    surface, config = materialize_grok_plugin_symlink(target)
    link = target / GROK_PLUGIN_DEST
    assert link.is_symlink()
    assert (link.resolve() / ".grok-plugin" / "plugin.json").is_file()
    assert surface["path"] == GROK_PLUGIN_DEST.as_posix()
    assert config["kind"] == "grok_plugin"