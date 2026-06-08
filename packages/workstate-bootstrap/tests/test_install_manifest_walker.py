"""WORKSTATE-REF-56 implementation note: manifest-driven hook walker.

The walker is the single mechanism that materializes harness hooks from
``config/agent-workflows/portable_commands.json`` (schema v2). It
replaces the bespoke ``_write_claude_settings_hooks`` Slice-1-of-plan-0008
writer. Tests pin:

* default install (no opt-in flag, no ``mcp_servers``) does NOT write any
  ``.claude/settings*.json`` from the walker — the harness Stop-hook is
  opt-in, the old "default-write-to-settings.local.json" behaviour is gone;
* ``install_claude_stop_hook=True`` materializes ONLY the shared
  ``.claude/settings.json`` adapter row from the manifest;
* ``install_claude_stop_hook_local=True`` materializes ONLY the
  ``.claude/settings.local.json`` adapter row;
* both flags together produce both files, each with one managed Stop entry;
* opting in when the manifest's ``required_artifacts`` are missing on disk
  is a hard fail (not a silent skip — the user asked for the hook and
  the system can't honour the ask);
* ``{{consumer_root}}`` substitution renders to the absolute target path;
* install.py does not hardcode ``.claude/settings`` strings — the manifest
  is the single source of truth for adapter targets.
"""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=30,
    ).stdout.strip()


def _init_git_repo(path: Path) -> None:
    _git("init", "--initial-branch=main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def _claude_stop_entry(command: str) -> dict:
    return {
        "_managed_by": "workstate-bootstrap",
        "matcher": "",
        "hooks": [{"type": "command", "command": command}],
    }


def _claude_post_tool_use_bash_entry(command: str) -> dict:
    return {
        "_managed_by": "workstate-bootstrap",
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": command, "timeout": 15}],
    }


def _assert_claude_post_tool_use_bash_entry(entry: dict, expected_cmd: str) -> None:
    assert entry["_managed_by"] == "workstate-bootstrap"
    assert entry["matcher"] == "Bash"
    assert entry["hooks"] == [
        {"type": "command", "command": expected_cmd, "timeout": 15}
    ]
    assert "command" not in entry


def _claude_session_start_entry(command: str) -> dict:
    # Same nested matcher+hooks[] managed shape as Stop entries — the
    # WS-REINJ-01 SessionStart family pins it via the schema contract test.
    return {
        "_managed_by": "workstate-bootstrap",
        "matcher": "",
        "hooks": [{"type": "command", "command": command}],
    }


def _assert_claude_stop_entry(entry: dict, expected_cmd: str) -> None:
    assert entry["_managed_by"] == "workstate-bootstrap"
    assert entry["matcher"] == ""
    assert entry["hooks"] == [{"type": "command", "command": expected_cmd}]
    assert "command" not in entry, "Claude Stop entries must use nested hooks[]"


def _build_manifest(*, with_artifact_on_disk: bool) -> dict:
    """Minimal v2 manifest carrying the compact-session hook with all
    five adapter rows: claude-code (shared + local), codex, vscode, and grok.
    """
    return {
        "version": 2,
        "commands": [],
        "hooks": [
            {
                "hook_id": "compact-session",
                "description": "Compact-session managed adapter.",
                "trigger": "stop",
                "required_artifacts": [
                    {
                        "kind": "file",
                        "consumer_path": "scripts/hooks/compact-session.py",
                    }
                ],
                "profiles": ["all"],
                "adapters": [
                    {
                        "harness": "claude-code",
                        "target": ".claude/settings.json",
                        "write_kind": "shared_checked_in",
                        "opt_in_flag": "--install-claude-stop-hook",
                        "patch": {
                            "operation": "merge_array_entry",
                            "json_path": "$.hooks.Stop",
                            "match_key": "_managed_by",
                            "entry": _claude_stop_entry(
                                "{{consumer_root}}/scripts/hooks/compact-session.py"
                            ),
                        },
                    },
                    {
                        "harness": "claude-code",
                        "target": ".claude/settings.local.json",
                        "write_kind": "user_owned_local",
                        "opt_in_flag": "--install-claude-stop-hook-local",
                        "patch": {
                            "operation": "merge_array_entry",
                            "json_path": "$.hooks.Stop",
                            "match_key": "_managed_by",
                            "entry": _claude_stop_entry(
                                "{{consumer_root}}/scripts/hooks/compact-session.py"
                            ),
                        },
                    },
                    {
                        "harness": "codex",
                        "target": ".codex/hooks/stop.json",
                        "write_kind": "shared_checked_in",
                        "opt_in_flag": "--install-codex-stop-hook",
                        "patch": {
                            "operation": "merge_array_entry",
                            "json_path": "$.hooks.Stop",
                            "match_key": "_managed_by",
                            "entry": {
                                "_managed_by": "workstate-bootstrap",
                                "command": "{{consumer_root}}/scripts/hooks/compact-session.py",
                            },
                        },
                    },
                    {
                        "harness": "vscode",
                        "target": ".vscode/workstate-stop-hooks.json",
                        "write_kind": "shared_checked_in",
                        "opt_in_flag": "--install-vscode-stop-hook",
                        "patch": {
                            "operation": "merge_array_entry",
                            "json_path": "$.hooks.Stop",
                            "match_key": "_managed_by",
                            "entry": {
                                "_managed_by": "workstate-bootstrap",
                                "command": "{{consumer_root}}/scripts/hooks/compact-session.py",
                            },
                        },
                    },
                    {
                        "harness": "grok",
                        "target": ".grok/hooks/stop.json",
                        "write_kind": "shared_checked_in",
                        "opt_in_flag": "--install-grok-stop-hook",
                        "patch": {
                            "operation": "merge_array_entry",
                            "json_path": "$.hooks.Stop",
                            "match_key": "_managed_by",
                            "entry": {
                                "_managed_by": "workstate-bootstrap",
                                "matcher": "",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "python3 \"{{consumer_root}}/scripts/hooks/compact-session.py\"",
                                        "env": {
                                            "WORKSTATE_HANDOFF_HARNESS": "grok"
                                        },
                                    }
                                ],
                            },
                        },
                    },
                ],
            },
            {
                "hook_id": "reinject-context",
                "description": "SessionStart re-injection managed adapter.",
                "trigger": "session-start",
                "supported_harnesses": ["claude-code"],
                "rationale": "Test stub: claude-code-only family.",
                "required_artifacts": [
                    {
                        "kind": "file",
                        "consumer_path": "scripts/hooks/reinject-context.py",
                    }
                ],
                "profiles": ["all"],
                "adapters": [
                    {
                        "harness": "claude-code",
                        "target": ".claude/settings.json",
                        "write_kind": "shared_checked_in",
                        "opt_in_flag": "--install-claude-reinject-hook",
                        "patch": {
                            "operation": "merge_array_entry",
                            "json_path": "$.hooks.SessionStart",
                            "match_key": "_managed_by",
                            "entry": _claude_session_start_entry(
                                "{{consumer_root}}/scripts/hooks/reinject-context.py"
                            ),
                        },
                    },
                    {
                        "harness": "claude-code",
                        "target": ".claude/settings.local.json",
                        "write_kind": "user_owned_local",
                        "opt_in_flag": "--install-claude-reinject-hook-local",
                        "patch": {
                            "operation": "merge_array_entry",
                            "json_path": "$.hooks.SessionStart",
                            "match_key": "_managed_by",
                            "entry": _claude_session_start_entry(
                                "{{consumer_root}}/scripts/hooks/reinject-context.py"
                            ),
                        },
                    },
                ],
            },
            {
                "hook_id": "capture-agent-errors",
                "description": "PostToolUse Bash error-capture managed adapter.",
                "trigger": "post-tool-use",
                "supported_harnesses": ["claude-code"],
                "rationale": "Test stub: claude-code-only family.",
                "required_artifacts": [
                    {
                        "kind": "file",
                        "consumer_path": "scripts/hooks/capture-agent-errors.py",
                    }
                ],
                "profiles": ["all"],
                "adapters": [
                    {
                        "harness": "claude-code",
                        "target": ".claude/settings.json",
                        "write_kind": "shared_checked_in",
                        "opt_in_flag": "--install-claude-error-hook",
                        "patch": {
                            "operation": "merge_array_entry",
                            "json_path": "$.hooks.PostToolUse",
                            "match_key": "_managed_by",
                            "entry": _claude_post_tool_use_bash_entry(
                                'python3 "$CLAUDE_PROJECT_DIR/scripts/hooks/capture-agent-errors.py"'
                            ),
                        },
                    },
                    {
                        "harness": "claude-code",
                        "target": ".claude/settings.local.json",
                        "write_kind": "user_owned_local",
                        "opt_in_flag": "--install-claude-error-hook-local",
                        "patch": {
                            "operation": "merge_array_entry",
                            "json_path": "$.hooks.PostToolUse",
                            "match_key": "_managed_by",
                            "entry": _claude_post_tool_use_bash_entry(
                                'python3 "$CLAUDE_PROJECT_DIR/scripts/hooks/capture-agent-errors.py"'
                            ),
                        },
                    },
                ],
            },
        ],
    }


@pytest.fixture()
def fake_remote_with_manifest(tmp_path: Path) -> tuple[str, str]:
    """Local bare remote shipping the v2 manifest under
    ``packages/workstate-system/`` plus the compact-session.py artifact at
    the path the manifest's required_artifacts row points to.
    """
    src = tmp_path / "manifest-src"
    src.mkdir()
    _init_git_repo(src)

    system = src / "packages" / "workstate-system"
    cfg_dir = system / "config" / "agent-workflows"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "portable_commands.json").write_text(
        json.dumps(_build_manifest(with_artifact_on_disk=True))
    )
    hooks_dir = system / "scripts" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "compact-session.py").write_text("#!/usr/bin/env python3\n")
    (hooks_dir / "compact-session.py").chmod(0o755)
    (hooks_dir / "reinject-context.py").write_text("#!/usr/bin/env python3\n")
    (hooks_dir / "reinject-context.py").chmod(0o755)
    (hooks_dir / "capture-agent-errors.py").write_text("#!/usr/bin/env python3\n")
    (hooks_dir / "capture-agent-errors.py").chmod(0o755)

    _git("add", "-A", cwd=src)
    _git("commit", "-m", "seed manifest+hook", cwd=src)
    _git("tag", "v0.1.0", cwd=src)

    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(src), str(bare), cwd=tmp_path)
    return f"file://{bare}", "v0.1.0"


@pytest.fixture()
def fake_remote_missing_artifact(tmp_path: Path) -> tuple[str, str]:
    """Same as ``fake_remote_with_manifest`` but the required artifact
    (compact-session.py) is deliberately NOT shipped, so opting in to
    the hook must hard-fail."""
    src = tmp_path / "missing-src"
    src.mkdir()
    _init_git_repo(src)

    system = src / "packages" / "workstate-system"
    cfg_dir = system / "config" / "agent-workflows"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "portable_commands.json").write_text(
        json.dumps(_build_manifest(with_artifact_on_disk=False))
    )

    _git("add", "-A", cwd=src)
    _git("commit", "-m", "seed manifest only", cwd=src)
    _git("tag", "v0.1.0", cwd=src)

    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(src), str(bare), cwd=tmp_path)
    return f"file://{bare}", "v0.1.0"


# ---------------------------------------------------------------------------
# Default install: NO opt-in flag -> walker writes nothing
# ---------------------------------------------------------------------------


def test_default_install_does_not_write_any_claude_settings(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the opt-in flags, the walker must NOT create either
    ``.claude/settings.json`` or ``.claude/settings.local.json`` —
    even though the manifest declares adapter rows for both, neither has
    been opted into."""
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    manifest = install(target=target, remote_url=url, remote_ref=ref)

    assert not (target / ".claude" / "settings.json").exists()
    assert not (target / ".claude" / "settings.local.json").exists()
    assert not (target / ".codex" / "hooks" / "stop.json").exists()
    assert not (target / ".vscode" / "workstate-stop-hooks.json").exists()

    configs = {entry["path"] for entry in manifest["configs"]}
    assert ".claude/settings.json" not in configs
    assert ".claude/settings.local.json" not in configs
    assert ".codex/hooks/stop.json" not in configs
    assert ".vscode/workstate-stop-hooks.json" not in configs


# ---------------------------------------------------------------------------
# --install-claude-stop-hook -> writes ONLY the shared settings.json
# ---------------------------------------------------------------------------


def test_install_claude_stop_hook_writes_only_shared_settings(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_stop_hook=True,
    )

    shared = target / ".claude" / "settings.json"
    local = target / ".claude" / "settings.local.json"
    assert shared.is_file()
    assert not local.exists()

    doc = json.loads(shared.read_text())
    stop_entries = doc["hooks"]["Stop"]
    assert len(stop_entries) == 1
    entry = stop_entries[0]
    # {{consumer_root}} must be rendered to the resolved target path.
    expected_cmd = str(target.resolve() / "scripts" / "hooks" / "compact-session.py")
    _assert_claude_stop_entry(entry, expected_cmd)

    configs = {entry["path"] for entry in manifest["configs"]}
    assert ".claude/settings.json" in configs


# ---------------------------------------------------------------------------
# --install-claude-stop-hook-local -> writes ONLY settings.local.json
# ---------------------------------------------------------------------------


def test_install_claude_stop_hook_local_writes_only_local_settings(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_stop_hook_local=True,
    )

    shared = target / ".claude" / "settings.json"
    local = target / ".claude" / "settings.local.json"
    assert local.is_file()
    assert not shared.exists()

    doc = json.loads(local.read_text())
    stop_entries = doc["hooks"]["Stop"]
    assert len(stop_entries) == 1
    expected_cmd = str(target.resolve() / "scripts" / "hooks" / "compact-session.py")
    _assert_claude_stop_entry(stop_entries[0], expected_cmd)

    configs = {entry["path"] for entry in manifest["configs"]}
    assert ".claude/settings.local.json" in configs


# ---------------------------------------------------------------------------
# Both flags -> both files
# ---------------------------------------------------------------------------


def test_both_flags_write_both_settings_files(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_stop_hook=True,
        install_claude_stop_hook_local=True,
    )

    shared = target / ".claude" / "settings.json"
    local = target / ".claude" / "settings.local.json"
    assert shared.is_file()
    assert local.is_file()
    for path in (shared, local):
        doc = json.loads(path.read_text())
        assert len(doc["hooks"]["Stop"]) == 1
        expected_cmd = str(
            target.resolve() / "scripts" / "hooks" / "compact-session.py"
        )
        _assert_claude_stop_entry(doc["hooks"]["Stop"][0], expected_cmd)


# ---------------------------------------------------------------------------
# --install-codex-stop-hook -> writes ONLY .codex/hooks/stop.json
# ---------------------------------------------------------------------------


def test_install_codex_stop_hook_writes_only_codex_target(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_codex_stop_hook=True,
    )

    codex_stop = target / ".codex" / "hooks" / "stop.json"
    assert codex_stop.is_file()
    assert not (target / ".claude" / "settings.json").exists()
    assert not (target / ".claude" / "settings.local.json").exists()
    assert not (target / ".vscode" / "workstate-stop-hooks.json").exists()

    doc = json.loads(codex_stop.read_text())
    stop_entries = doc["hooks"]["Stop"]
    assert len(stop_entries) == 1
    entry = stop_entries[0]
    assert entry["_managed_by"] == "workstate-bootstrap"
    expected_cmd = str(target.resolve() / "scripts" / "hooks" / "compact-session.py")
    assert entry["command"] == expected_cmd

    configs = {entry["path"] for entry in manifest["configs"]}
    assert ".codex/hooks/stop.json" in configs


# ---------------------------------------------------------------------------
# --install-vscode-stop-hook -> writes ONLY .vscode/workstate-stop-hooks.json
# ---------------------------------------------------------------------------


def test_install_vscode_stop_hook_writes_only_vscode_target(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_vscode_stop_hook=True,
    )

    vscode_stop = target / ".vscode" / "workstate-stop-hooks.json"
    assert vscode_stop.is_file()
    assert not (target / ".claude" / "settings.json").exists()
    assert not (target / ".claude" / "settings.local.json").exists()
    assert not (target / ".codex" / "hooks" / "stop.json").exists()

    doc = json.loads(vscode_stop.read_text())
    stop_entries = doc["hooks"]["Stop"]
    assert len(stop_entries) == 1
    assert stop_entries[0]["_managed_by"] == "workstate-bootstrap"

    configs = {entry["path"] for entry in manifest["configs"]}
    assert ".vscode/workstate-stop-hooks.json" in configs


# ---------------------------------------------------------------------------
# --install-grok-stop-hook -> writes ONLY .grok/hooks/stop.json
# ---------------------------------------------------------------------------


def test_install_grok_stop_hook_writes_only_grok_target(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_grok_stop_hook=True,
    )

    grok_stop = target / ".grok" / "hooks" / "stop.json"
    assert grok_stop.is_file()
    assert not (target / ".claude" / "settings.json").exists()
    assert not (target / ".codex" / "hooks" / "stop.json").exists()
    assert not (target / ".vscode" / "workstate-stop-hooks.json").exists()

    doc = json.loads(grok_stop.read_text())
    stop_entries = doc["hooks"]["Stop"]
    assert len(stop_entries) == 1
    entry = stop_entries[0]
    assert entry["_managed_by"] == "workstate-bootstrap"
    assert entry["hooks"][0]["env"]["WORKSTATE_HANDOFF_HARNESS"] == "grok"
    expected_cmd = (
        f'python3 "{target.resolve() / "scripts" / "hooks" / "compact-session.py"}"'
    )
    assert entry["hooks"][0]["command"] == expected_cmd

    configs = {entry["path"] for entry in manifest["configs"]}
    assert ".grok/hooks/stop.json" in configs


# ---------------------------------------------------------------------------
# All five flags -> all five files, each carrying exactly one managed entry
# ---------------------------------------------------------------------------


def test_all_five_flags_write_all_five_adapter_targets(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_stop_hook=True,
        install_claude_stop_hook_local=True,
        install_codex_stop_hook=True,
        install_vscode_stop_hook=True,
        install_grok_stop_hook=True,
    )

    written = [
        target / ".claude" / "settings.json",
        target / ".claude" / "settings.local.json",
        target / ".codex" / "hooks" / "stop.json",
        target / ".vscode" / "workstate-stop-hooks.json",
        target / ".grok" / "hooks" / "stop.json",
    ]
    for path in written:
        assert path.is_file(), path
        doc = json.loads(path.read_text())
        assert len(doc["hooks"]["Stop"]) == 1
        assert doc["hooks"]["Stop"][0]["_managed_by"] == "workstate-bootstrap"
        if path.name.startswith("settings"):
            expected_cmd = str(
                target.resolve() / "scripts" / "hooks" / "compact-session.py"
            )
            _assert_claude_stop_entry(doc["hooks"]["Stop"][0], expected_cmd)


# ---------------------------------------------------------------------------
# Opt-in + missing artifact -> hard fail
# ---------------------------------------------------------------------------


def test_opt_in_with_missing_artifact_hard_fails(
    tmp_path: Path,
    fake_remote_missing_artifact: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opting in to a hook whose ``required_artifacts`` are not on disk
    in the clone must raise rather than silently skip the patch. The
    user asked for the hook; we can't honour the ask without the script."""
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_missing_artifact

    with pytest.raises(RuntimeError, match="compact-session"):
        install(
            target=target,
            remote_url=url,
            remote_ref=ref,
            install_claude_stop_hook_local=True,
        )


# ---------------------------------------------------------------------------
# Idempotency: re-running install with the same flag does not duplicate
# ---------------------------------------------------------------------------


def test_walker_is_idempotent_on_rerun(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_stop_hook_local=True,
    )
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_stop_hook_local=True,
    )

    doc = json.loads((target / ".claude" / "settings.local.json").read_text())
    stop_entries = doc["hooks"]["Stop"]
    assert len(stop_entries) == 1, "second install must not duplicate the managed entry"


# ---------------------------------------------------------------------------
# Pre-existing unrelated Stop entry survives the merge
# ---------------------------------------------------------------------------


def test_walker_preserves_unrelated_stop_entries(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    claude_dir = target / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"command": "/usr/local/bin/my-custom-hook.sh"},
                    ]
                },
                "other_top_level": "preserved",
            }
        )
    )

    url, ref = fake_remote_with_manifest
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_stop_hook_local=True,
    )

    doc = json.loads((claude_dir / "settings.local.json").read_text())
    assert doc["other_top_level"] == "preserved"
    commands = {e.get("command") for e in doc["hooks"]["Stop"]}
    assert "/usr/local/bin/my-custom-hook.sh" in commands
    managed = [
        e for e in doc["hooks"]["Stop"] if e.get("_managed_by") == "workstate-bootstrap"
    ]
    assert len(managed) == 1


# ---------------------------------------------------------------------------
# Pre-existing OLD flat managed entry is upgraded to the nested shape
# ---------------------------------------------------------------------------


def test_walker_upgrades_stale_flat_managed_stop_entry(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer carrying the pre-fix FLAT managed entry (the shape Claude
    Code silently ignores) must have it replaced in place with the nested
    ``matcher`` + ``hooks[]`` shape on re-install: exactly one managed entry
    afterwards, no stale top-level ``command`` key surviving the merge, and
    unmanaged neighbours untouched."""
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    claude_dir = target / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "_managed_by": "workstate-bootstrap",
                            "command": "/old/flat/compact-session.py",
                        },
                        {"command": "/usr/local/bin/my-custom-hook.sh"},
                    ]
                }
            }
        )
    )

    url, ref = fake_remote_with_manifest
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_stop_hook_local=True,
    )

    doc = json.loads((claude_dir / "settings.local.json").read_text())
    managed = [
        e for e in doc["hooks"]["Stop"] if e.get("_managed_by") == "workstate-bootstrap"
    ]
    assert len(managed) == 1, "stale flat entry must be replaced, not duplicated"
    expected_cmd = str(target.resolve() / "scripts" / "hooks" / "compact-session.py")
    _assert_claude_stop_entry(managed[0], expected_cmd)
    # The unmanaged neighbour survives the upgrade untouched.
    assert {"command": "/usr/local/bin/my-custom-hook.sh"} in doc["hooks"]["Stop"]


# ---------------------------------------------------------------------------
# Source-greppability: install.py does not hardcode .claude/settings paths.
# ---------------------------------------------------------------------------


def test_install_py_does_not_hardcode_claude_settings_paths() -> None:
    """Manifest is the single source of truth. Greppability check ensures
    the walker truly drives off the manifest rather than carrying a
    parallel hardcoded mirror of adapter targets.

    Docstrings are stripped before checking — the rule targets code-path
    logic, not parameter documentation that legitimately names the target
    file for each opt-in stop-hook flag.
    """
    import ast

    install_py = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "workstate_bootstrap"
        / "install.py"
    )
    tree = ast.parse(install_py.read_text())
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body[0].value.value = ""
    text = ast.unparse(tree)
    assert ".claude/settings.json" not in text, (
        "install.py must not hardcode '.claude/settings.json' — drive from the manifest"
    )
    assert ".claude/settings.local.json" not in text, (
        "install.py must not hardcode '.claude/settings.local.json' — drive from the manifest"
    )


# ---------------------------------------------------------------------------
# WORKSTATE-REF-80 implementation note: bootstrap doctor/repair for installed Stop adapters.
#
# A Stop adapter that bootstrap actually installed (its target appears in the
# install manifest's ``configs``) is a managed surface: doctor must flag it
# when the managed entry is missing or no longer matches the manifest-declared
# ``compact-session`` entry, and repair must restore it. A never-installed
# adapter stays optional — doctor must NOT turn it into drift.
# ---------------------------------------------------------------------------


_VSCODE_STOP_TARGET = ".vscode/workstate-stop-hooks.json"


def _hook_findings(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["kind"] == "hook_adapter_drift"]


def test_doctor_clean_for_installed_managed_stop_adapter(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An adapter installed via opt-in flag, left untouched, is clean."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_vscode_stop_hook=True,
    )

    findings = doctor(target=target)
    assert _hook_findings(findings) == []


def test_doctor_clean_when_stop_adapter_never_installed(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never-installed adapters are optional, not drift."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    install(target=target, remote_url=url, remote_ref=ref)

    assert not (target / _VSCODE_STOP_TARGET).exists()
    findings = doctor(target=target)
    assert _hook_findings(findings) == []


def test_doctor_flags_drift_when_installed_stop_adapter_file_removed(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting an installed adapter file is managed-surface drift."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_vscode_stop_hook=True,
    )
    (target / _VSCODE_STOP_TARGET).unlink()

    hook_findings = _hook_findings(doctor(target=target))
    assert any(f["path"] == _VSCODE_STOP_TARGET for f in hook_findings)


def test_doctor_flags_drift_when_managed_stop_entry_edited(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editing the managed entry away from the manifest-declared shape is drift."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_vscode_stop_hook=True,
    )
    adapter_path = target / _VSCODE_STOP_TARGET
    doc = json.loads(adapter_path.read_text())
    doc["hooks"]["Stop"][0]["command"] = "/tmp/hijacked-hook.sh"
    adapter_path.write_text(json.dumps(doc))

    hook_findings = _hook_findings(doctor(target=target))
    assert any(f["path"] == _VSCODE_STOP_TARGET for f in hook_findings)


def test_repair_restores_drifted_managed_stop_adapter(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair re-applies the manifest-declared managed entry for a drifted
    adapter and leaves doctor clean."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor, repair

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_vscode_stop_hook=True,
    )
    (target / _VSCODE_STOP_TARGET).unlink()
    assert _hook_findings(doctor(target=target))  # drift before repair

    report = repair(target=target)
    assert any(
        f["kind"] == "hook_adapter_drift" and f["path"] == _VSCODE_STOP_TARGET
        for f in report["repaired"]
    )

    restored = json.loads((target / _VSCODE_STOP_TARGET).read_text())
    managed = [
        e
        for e in restored["hooks"]["Stop"]
        if e.get("_managed_by") == "workstate-bootstrap"
    ]
    assert len(managed) == 1
    expected_cmd = str(target.resolve() / "scripts" / "hooks" / "compact-session.py")
    assert managed[0]["command"] == expected_cmd
    assert _hook_findings(doctor(target=target)) == []


# ---------------------------------------------------------------------------
# WS-HOOKOPTIN-01: hook adapter rows are kind-tagged and survive update()
# ---------------------------------------------------------------------------


def _hook_adapter_rows(manifest: dict) -> list[dict]:
    return [
        row
        for row in manifest["configs"]
        if isinstance(row, dict) and row.get("kind") == "hook_adapter"
    ]


def test_hook_adapter_config_rows_are_tagged_with_opt_in_flag(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manifest config rows written by the hook walker carry
    ``kind=hook_adapter`` + ``opt_in_flag`` so doctor and update can
    distinguish them from unrelated config rows that share the same path
    (e.g. ``.claude/settings.json`` written by the settings writer)."""
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_stop_hook=True,
        install_vscode_stop_hook=True,
    )

    rows = {row["path"]: row for row in _hook_adapter_rows(manifest)}
    assert set(rows) == {".claude/settings.json", _VSCODE_STOP_TARGET}
    assert rows[".claude/settings.json"]["opt_in_flag"] == "--install-claude-stop-hook"
    assert rows[_VSCODE_STOP_TARGET]["opt_in_flag"] == "--install-vscode-stop-hook"


def test_update_preserves_hook_adapter_opt_ins(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``update`` re-derives opt-in flags from the recorded manifest rows so
    a refresh re-applies the managed Stop adapter instead of silently
    dropping it from management (the pre-WS-HOOKOPTIN-01 behavior)."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import update

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_vscode_stop_hook=True,
    )
    # Simulate drift between refreshes: the adapter file is gone entirely.
    (target / _VSCODE_STOP_TARGET).unlink()

    manifest = update(
        target=target,
        remote_ref=ref,
        enforce_required_surfaces=False,
        adopt_redundant=False,
    )

    restored = json.loads((target / _VSCODE_STOP_TARGET).read_text())
    managed = [
        e
        for e in restored["hooks"]["Stop"]
        if e.get("_managed_by") == "workstate-bootstrap"
    ]
    assert len(managed) == 1
    rows = {row["path"]: row for row in _hook_adapter_rows(manifest)}
    assert _VSCODE_STOP_TARGET in rows
    assert rows[_VSCODE_STOP_TARGET]["opt_in_flag"] == "--install-vscode-stop-hook"


def test_update_without_prior_opt_in_writes_no_adapters(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never-installed adapters stay optional across update: no adapter file
    materializes and no hook_adapter rows appear in the refreshed manifest."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import update

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    install(target=target, remote_url=url, remote_ref=ref)

    manifest = update(
        target=target,
        remote_ref=ref,
        enforce_required_surfaces=False,
        adopt_redundant=False,
    )

    assert not (target / _VSCODE_STOP_TARGET).exists()
    assert not (target / ".codex" / "hooks" / "stop.json").exists()
    assert _hook_adapter_rows(manifest) == []


# ---------------------------------------------------------------------------
# WS-REINJ-01 implementation note: reinject-context SessionStart family — opt-in writes,
# per-flag manifest rows on a shared path, update preservation, doctor/repair.
# ---------------------------------------------------------------------------


def _assert_claude_session_start_entry(entry: dict, expected_cmd: str) -> None:
    assert entry["_managed_by"] == "workstate-bootstrap"
    assert entry["matcher"] == ""
    assert entry["hooks"] == [{"type": "command", "command": expected_cmd}]
    assert "command" not in entry, "Claude SessionStart entries must use nested hooks[]"


def test_install_claude_reinject_hook_writes_only_shared_sessionstart_entry(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reinject opt-in writes ONLY the managed ``$.hooks.SessionStart``
    entry in the shared settings file — no Stop entry, no local file."""
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_reinject_hook=True,
    )

    shared = target / ".claude" / "settings.json"
    assert shared.is_file()
    assert not (target / ".claude" / "settings.local.json").exists()

    doc = json.loads(shared.read_text())
    session_entries = doc["hooks"]["SessionStart"]
    assert len(session_entries) == 1
    expected_cmd = str(target.resolve() / "scripts" / "hooks" / "reinject-context.py")
    _assert_claude_session_start_entry(session_entries[0], expected_cmd)
    assert "Stop" not in doc["hooks"], "reinject opt-in must not write a Stop entry"

    rows = _hook_adapter_rows(manifest)
    assert [(row["path"], row["opt_in_flag"]) for row in rows] == [
        (".claude/settings.json", "--install-claude-reinject-hook")
    ]


def test_install_claude_reinject_hook_local_writes_only_local_settings(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_reinject_hook_local=True,
    )

    local = target / ".claude" / "settings.local.json"
    assert local.is_file()
    assert not (target / ".claude" / "settings.json").exists()

    doc = json.loads(local.read_text())
    expected_cmd = str(target.resolve() / "scripts" / "hooks" / "reinject-context.py")
    _assert_claude_session_start_entry(doc["hooks"]["SessionStart"][0], expected_cmd)


def test_reinject_and_stop_hooks_share_settings_file_without_clobbering(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both families opt into the SAME shared settings.json: the merged file
    carries one managed Stop entry AND one managed SessionStart entry, and
    the manifest records one tagged row per opt-in flag (path is not a
    unique key for hook_adapter rows)."""
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_stop_hook=True,
        install_claude_reinject_hook=True,
    )

    doc = json.loads((target / ".claude" / "settings.json").read_text())
    assert len(doc["hooks"]["Stop"]) == 1
    assert len(doc["hooks"]["SessionStart"]) == 1
    _assert_claude_stop_entry(
        doc["hooks"]["Stop"][0],
        str(target.resolve() / "scripts" / "hooks" / "compact-session.py"),
    )
    _assert_claude_session_start_entry(
        doc["hooks"]["SessionStart"][0],
        str(target.resolve() / "scripts" / "hooks" / "reinject-context.py"),
    )

    flags = sorted(
        row["opt_in_flag"]
        for row in _hook_adapter_rows(manifest)
        if row["path"] == ".claude/settings.json"
    )
    assert flags == [
        "--install-claude-reinject-hook",
        "--install-claude-stop-hook",
    ]


def test_update_preserves_reinject_opt_in(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``update`` re-derives the reinject opt-in from the tagged manifest row
    (``_HOOK_OPT_IN_KWARGS`` registration), re-applying the managed
    SessionStart entry instead of silently dropping the family."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import update

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_reinject_hook=True,
    )
    (target / ".claude" / "settings.json").unlink()

    manifest = update(
        target=target,
        remote_ref=ref,
        enforce_required_surfaces=False,
        adopt_redundant=False,
    )

    restored = json.loads((target / ".claude" / "settings.json").read_text())
    managed = [
        e
        for e in restored["hooks"]["SessionStart"]
        if e.get("_managed_by") == "workstate-bootstrap"
    ]
    assert len(managed) == 1
    flags = {row["opt_in_flag"] for row in _hook_adapter_rows(manifest)}
    assert "--install-claude-reinject-hook" in flags


def test_doctor_and_repair_cover_managed_reinject_entry(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editing the managed SessionStart entry is hook_adapter_drift; repair
    restores it from the manifest declaration."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor, repair

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_reinject_hook=True,
    )
    settings = target / ".claude" / "settings.json"
    doc = json.loads(settings.read_text())
    doc["hooks"]["SessionStart"][0]["hooks"] = [
        {"type": "command", "command": "/tmp/hijacked-reinject.sh"}
    ]
    settings.write_text(json.dumps(doc))

    hook_findings = _hook_findings(doctor(target=target))
    assert any(f["path"] == ".claude/settings.json" for f in hook_findings)

    report = repair(target=target)
    assert any(
        f["kind"] == "hook_adapter_drift" and f["path"] == ".claude/settings.json"
        for f in report["repaired"]
    )
    restored = json.loads(settings.read_text())
    expected_cmd = str(target.resolve() / "scripts" / "hooks" / "reinject-context.py")
    _assert_claude_session_start_entry(
        restored["hooks"]["SessionStart"][0], expected_cmd
    )
    assert _hook_findings(doctor(target=target)) == []


def test_doctor_does_not_flag_uninstalled_sibling_family_on_shared_path(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the reinject family was installed on ``.claude/settings.json``:
    the never-opted-in compact-session shared adapter targeting the SAME
    path must stay optional, not read as drift (managed-ness is per
    opt-in flag, not per path)."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_reinject_hook=True,
    )

    assert _hook_findings(doctor(target=target)) == []


# ---------------------------------------------------------------------------
# implementation note implementation note: capture-agent-errors PostToolUse family
# ---------------------------------------------------------------------------


def test_install_claude_error_hook_writes_only_shared_posttooluse_entry(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_error_hook=True,
    )

    shared = target / ".claude" / "settings.json"
    local = target / ".claude" / "settings.local.json"
    assert shared.is_file()
    assert not local.exists()

    doc = json.loads(shared.read_text())
    post_entries = doc["hooks"]["PostToolUse"]
    assert len(post_entries) == 1
    expected_cmd = (
        'python3 "$CLAUDE_PROJECT_DIR/scripts/hooks/capture-agent-errors.py"'
    )
    _assert_claude_post_tool_use_bash_entry(post_entries[0], expected_cmd)

    rows = {row["path"]: row for row in _hook_adapter_rows(manifest)}
    assert rows[".claude/settings.json"]["opt_in_flag"] == "--install-claude-error-hook"


def test_error_and_stop_hooks_share_settings_without_clobbering(
    tmp_path: Path,
    fake_remote_with_manifest: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import install

    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_manifest

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        install_claude_stop_hook=True,
        install_claude_error_hook=True,
    )

    doc = json.loads((target / ".claude" / "settings.json").read_text())
    assert len(doc["hooks"]["Stop"]) == 1
    assert len(doc["hooks"]["PostToolUse"]) == 1
    flags = sorted(
        row["opt_in_flag"]
        for row in _hook_adapter_rows(manifest)
        if row["path"] == ".claude/settings.json"
    )
    assert flags == [
        "--install-claude-error-hook",
        "--install-claude-stop-hook",
    ]
