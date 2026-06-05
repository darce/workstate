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


def _assert_claude_stop_entry(entry: dict, expected_cmd: str) -> None:
    assert entry["_managed_by"] == "workstate-bootstrap"
    assert entry["matcher"] == ""
    assert entry["hooks"] == [{"type": "command", "command": expected_cmd}]
    assert "command" not in entry, "Claude Stop entries must use nested hooks[]"


def _build_manifest(*, with_artifact_on_disk: bool) -> dict:
    """Minimal v2 manifest carrying the compact-session hook with all
    four adapter rows: claude-code (shared + local), codex, and vscode.
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
                ],
            }
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
        expected_cmd = str(target.resolve() / "scripts" / "hooks" / "compact-session.py")
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
# All four flags -> all four files, each carrying exactly one managed entry
# ---------------------------------------------------------------------------


def test_all_four_flags_write_all_four_adapter_targets(
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
    )

    written = [
        target / ".claude" / "settings.json",
        target / ".claude" / "settings.local.json",
        target / ".codex" / "hooks" / "stop.json",
        target / ".vscode" / "workstate-stop-hooks.json",
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
    managed = [e for e in doc["hooks"]["Stop"] if e.get("_managed_by") == "workstate-bootstrap"]
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

    install_py = Path(__file__).resolve().parents[1] / "src" / "workstate_bootstrap" / "install.py"
    tree = ast.parse(install_py.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
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
        e for e in restored["hooks"]["Stop"]
        if e.get("_managed_by") == "workstate-bootstrap"
    ]
    assert len(managed) == 1
    expected_cmd = str(target.resolve() / "scripts" / "hooks" / "compact-session.py")
    assert managed[0]["command"] == expected_cmd
    assert _hook_findings(doctor(target=target)) == []
