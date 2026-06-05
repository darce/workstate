"""WORKSTATE-REF-02 follow-up: lint for ``.claude/settings.json`` pin contract.

The Claude pin file is a tiny, hand-edited JSON document, but its
``extraKnownMarketplaces.<name>.source.source`` discriminator must be
``directory`` (Claude Code 2.1.144+); the Codex marketplace catalog
uses a different ``source`` shape, and pasting one into the other
silently breaks Claude session start. There is no native schema check,
so this validator is the only guard against regressions.

Rules enforced:

1. Every ``extraKnownMarketplaces.<name>.source.source`` MUST be
   ``"directory"``.
2. Every ``extraKnownMarketplaces.<name>.source.path`` MUST be a
   relative path; absolute paths (typically inserted by
   ``claude plugin marketplace add --scope project``) break
   portability across clones.
3. Every key in ``enabledPlugins`` has the form ``<plugin>@<market>``
   where ``<market>`` is declared in ``extraKnownMarketplaces``.
4. The committed ``.claude/settings.json`` at the repo root passes
   every rule above.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from validate_claude_settings_pin import (  # type: ignore[import-not-found]
    SettingsPinError,
    validate_settings,
    main as cli_main,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
WORKTREE_ROOT = PACKAGE_ROOT.parents[1]
REPO_SETTINGS = WORKTREE_ROOT / ".claude" / "settings.json"


def _valid_settings() -> dict:
    return {
        "extraKnownMarketplaces": {
            "workstate-marketplace": {
                "source": {"source": "directory", "path": "."}
            }
        },
        "enabledPlugins": {
            "workstate-system@workstate-marketplace": True,
        },
    }


def _write_pin_files(
    repo_root: Path,
    *,
    plugin_source: str = "./.workstate/generated/plugins/workstate-system/base/claude",
    create_plugin_dir: bool = True,
) -> Path:
    (repo_root / ".claude").mkdir(parents=True, exist_ok=True)
    (repo_root / ".claude-plugin").mkdir(parents=True, exist_ok=True)

    settings_path = repo_root / ".claude" / "settings.json"
    settings_path.write_text(json.dumps(_valid_settings()))
    (repo_root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "workstate-marketplace",
                "owner": {"name": "workstate maintainers"},
                "plugins": [
                    {
                        "name": "workstate-system",
                        "source": plugin_source,
                        "description": "Test plugin pin.",
                    }
                ],
            }
        )
    )
    if create_plugin_dir:
        (repo_root / plugin_source.removeprefix("./")).mkdir(parents=True, exist_ok=True)
    return settings_path


def test_valid_settings_pass() -> None:
    assert validate_settings(_valid_settings()) == []


def test_effective_tree_pin_passes_when_present(tmp_path: Path) -> None:
    settings_path = _write_pin_files(
        tmp_path,
        plugin_source="./.workstate/generated/plugins/workstate-system/effective/claude",
    )
    parsed = json.loads(settings_path.read_text())
    assert validate_settings(parsed, settings_path=settings_path) == []


def test_missing_effective_tree_is_reported(tmp_path: Path) -> None:
    settings_path = _write_pin_files(
        tmp_path,
        plugin_source="./.workstate/generated/plugins/workstate-system/effective/claude",
        create_plugin_dir=False,
    )
    parsed = json.loads(settings_path.read_text())

    errors = validate_settings(parsed, settings_path=settings_path)
    assert errors, "must reject a pin whose effective tree is missing"
    assert any("install" in e.lower() or "update" in e.lower() for e in errors), errors


def test_missing_marketplace_file_does_not_fail_settings_only_validation(tmp_path: Path) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(_valid_settings()))

    parsed = json.loads(settings_path.read_text())

    assert validate_settings(parsed, settings_path=settings_path) == []


def test_rejects_marketplace_source_path_traversal(tmp_path: Path) -> None:
    settings_path = _write_pin_files(tmp_path, plugin_source="../outside/claude")
    parsed = json.loads(settings_path.read_text())

    errors = validate_settings(parsed, settings_path=settings_path)

    assert errors, "must reject marketplace plugin sources that traverse outside the repo"
    assert any("travers" in e.lower() or "outside" in e.lower() for e in errors), errors


def test_rejects_codex_local_discriminator() -> None:
    bad = _valid_settings()
    bad["extraKnownMarketplaces"]["workstate-marketplace"]["source"]["source"] = "local"
    errors = validate_settings(bad)
    assert errors, "must reject Codex-style 'local' discriminator"
    assert any("directory" in e and "local" in e for e in errors), errors


def test_rejects_absolute_path() -> None:
    bad = _valid_settings()
    bad["extraKnownMarketplaces"]["workstate-marketplace"]["source"]["path"] = "/Users/foo/repo"
    errors = validate_settings(bad)
    assert errors, "must reject absolute marketplace source path"
    assert any("absolute" in e.lower() or "relative" in e.lower() for e in errors), errors


def test_rejects_enabled_plugin_with_undeclared_marketplace() -> None:
    bad = _valid_settings()
    bad["enabledPlugins"]["other-plugin@ghost-marketplace"] = True
    errors = validate_settings(bad)
    assert errors, "must reject enabled plugin referencing undeclared marketplace"
    assert any("ghost-marketplace" in e for e in errors), errors


def test_rejects_enabled_plugin_missing_at_sign() -> None:
    bad = _valid_settings()
    bad["enabledPlugins"]["broken-no-at-sign"] = True
    errors = validate_settings(bad)
    assert errors, "must reject enabledPlugins key without <plugin>@<market> form"


def test_committed_repo_settings_pass() -> None:
    """The committed repo-root pin must pass every rule. If this fails
    after a settings edit, fix the pin — do not loosen the validator."""
    assert REPO_SETTINGS.is_file(), f"missing committed pin: {REPO_SETTINGS}"
    parsed = json.loads(REPO_SETTINGS.read_text())
    errors = validate_settings(parsed, settings_path=REPO_SETTINGS)
    assert errors == [], f"committed pin failed validation: {errors}"


def test_committed_settings_wire_fresh_clone_plugin_hook() -> None:
    """The committed pin must register the SessionStart hook that
    regenerates the gitignored plugin tree on a fresh clone, and the
    referenced script must exist and be executable. Without this, a fresh
    clone silently has no slash commands until someone runs plugins-build
    by hand — the exact regression this hook guards against."""
    parsed = json.loads(REPO_SETTINGS.read_text())
    session_start = parsed.get("hooks", {}).get("SessionStart", [])
    commands = [
        hook.get("command", "")
        for group in session_start
        for hook in group.get("hooks", [])
    ]
    hook_rel = ".claude/hooks/ensure-agent-surfaces.sh"
    assert any(hook_rel in cmd for cmd in commands), (
        f"SessionStart must invoke {hook_rel}; got {commands}"
    )
    script = WORKTREE_ROOT / hook_rel
    assert script.is_file(), f"missing hook script: {script}"
    assert os.access(script, os.X_OK), f"hook script not executable: {script}"


def test_cli_exit_code_on_invalid(tmp_path, capsys) -> None:
    bad = _valid_settings()
    bad["extraKnownMarketplaces"]["workstate-marketplace"]["source"]["source"] = "local"
    settings = _write_pin_files(tmp_path)
    settings.write_text(json.dumps(bad))

    rc = cli_main([str(settings)])
    captured = capsys.readouterr()
    assert rc == 1, captured
    assert "directory" in captured.err


def test_cli_exit_code_on_valid(tmp_path, capsys) -> None:
    settings = _write_pin_files(tmp_path)

    rc = cli_main([str(settings)])
    assert rc == 0


def test_settings_pin_error_class_exists() -> None:
    assert issubclass(SettingsPinError, Exception)
