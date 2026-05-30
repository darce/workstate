from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
SOURCE_RELEASE_PREPARE = REPO_ROOT / "scripts" / "release_prepare.py"


def _build_prepare_fixture(tmp_path: Path, *, version: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "config" / "release").mkdir(parents=True)
    (repo / "scripts").mkdir(parents=True)
    package_dir = repo / "packages" / "workstate-protocol"
    package_dir.mkdir(parents=True)
    dependent_dir = repo / "packages" / "mcp-workstate-handoff"
    dependent_dir.mkdir(parents=True)

    (package_dir / "pyproject.toml").write_text(
        f"[project]\nname = \"workstate-protocol\"\nversion = \"{version}\"\n",
        encoding="utf-8",
    )
    (package_dir / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n",
        encoding="utf-8",
    )
    (dependent_dir / "pyproject.toml").write_text(
        (
            "[project]\n"
            'name = "mcp-workstate-handoff"\n'
            'version = "9.9.9"\n'
            "dependencies = [\n"
            f'    "workstate-protocol>={version},<2.0.0",\n'
            "]\n"
        ),
        encoding="utf-8",
    )
    (dependent_dir / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n",
        encoding="utf-8",
    )
    (repo / "config" / "release" / "packages.json").write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "name": "workstate-protocol",
                        "path": "packages/workstate-protocol",
                        "distribution": "workstate-protocol",
                        "artifact_prefix": "workstate_protocol",
                        "publish": True,
                        "test_command": "python -m pytest -q",
                        "changelog": "packages/workstate-protocol/CHANGELOG.md",
                    },
                    {
                        "name": "mcp-workstate-handoff",
                        "path": "packages/mcp-workstate-handoff",
                        "distribution": "mcp-workstate-handoff",
                        "artifact_prefix": "mcp_workstate_handoff",
                        "publish": True,
                        "test_command": "python -m pytest -q",
                        "changelog": "packages/mcp-workstate-handoff/CHANGELOG.md",
                    }
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / "scripts" / "release_prepare.py").write_text(
        SOURCE_RELEASE_PREPARE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return repo


def test_release_prepare_updates_version_and_changelog(tmp_path: Path) -> None:
    repo = _build_prepare_fixture(tmp_path, version="1.2.3")

    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "release_prepare.py"), "workstate-protocol", "1.2.4", "--date", "2026-05-21"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    pyproject = (repo / "packages" / "workstate-protocol" / "pyproject.toml").read_text(encoding="utf-8")
    changelog = (repo / "packages" / "workstate-protocol" / "CHANGELOG.md").read_text(encoding="utf-8")
    dependent_pyproject = (repo / "packages" / "mcp-workstate-handoff" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "1.2.4"' in pyproject
    assert "## [1.2.4] — 2026-05-21" in changelog
    assert "### Changed" in changelog
    assert '"workstate-protocol>=1.2.4,<2.0.0"' in dependent_pyproject


def test_release_prepare_runs_from_package_subdirectory(tmp_path: Path) -> None:
    repo = _build_prepare_fixture(tmp_path, version="1.2.3")
    subdir = repo / "packages" / "workstate-protocol"

    result = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "release_prepare.py"),
            "workstate-protocol",
            "patch",
            "--allow-dirty",
            "--dry-run",
            "--date",
            "2026-05-21",
        ],
        cwd=subdir,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Would bump workstate-protocol: 1.2.3 -> 1.2.4" in result.stdout


def test_release_prepare_dry_run_leaves_files_unchanged(tmp_path: Path) -> None:
    repo = _build_prepare_fixture(tmp_path, version="1.2.3")
    pyproject_path = repo / "packages" / "workstate-protocol" / "pyproject.toml"
    changelog_path = repo / "packages" / "workstate-protocol" / "CHANGELOG.md"
    pyproject_before = pyproject_path.read_text(encoding="utf-8")
    changelog_before = changelog_path.read_text(encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "release_prepare.py"),
            "workstate-protocol",
            "patch",
            "--dry-run",
            "--date",
            "2026-05-21",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert pyproject_path.read_text(encoding="utf-8") == pyproject_before
    assert changelog_path.read_text(encoding="utf-8") == changelog_before
    assert "Would bump workstate-protocol: 1.2.3 -> 1.2.4" in result.stdout