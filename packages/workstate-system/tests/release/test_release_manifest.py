from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
MANIFEST_HELPER = REPO_ROOT / "scripts" / "release_manifest.py"
SOURCE_MANIFEST = REPO_ROOT / "config" / "release" / "packages.json"


def _run_helper(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MANIFEST_HELPER), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_release_manifest_lists_all_packages_and_release_packages() -> None:
    all_packages = _run_helper("list", "--field", "name")
    assert all_packages.returncode == 0, all_packages.stderr
    assert all_packages.stdout.splitlines() == [
        "workstate-protocol",
        "mcp-workstate-handoff",
        "mcp-workstate-orchestrator",
        "mcp-workstate-canvas",
        "workstate-bootstrap",
        "workstate-codex-bridge",
        "workstate-system",
    ]

    release_packages = _run_helper("list", "--release-only", "--field", "name")
    assert release_packages.returncode == 0, release_packages.stderr
    assert release_packages.stdout.splitlines() == [
        "workstate-protocol",
        "mcp-workstate-handoff",
        "mcp-workstate-orchestrator",
        "workstate-bootstrap",
        "workstate-codex-bridge",
        "workstate-system",
    ]


def test_release_manifest_validates_and_exposes_artifact_prefixes() -> None:
    validate = _run_helper("validate")
    assert validate.returncode == 0, validate.stderr

    artifact_prefix = _run_helper("get", "mcp-workstate-handoff", "artifact_prefix")
    assert artifact_prefix.returncode == 0, artifact_prefix.stderr
    assert artifact_prefix.stdout.strip() == "mcp_workstate_handoff"


def _build_drift_fixture(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "config" / "release").mkdir(parents=True)
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(MANIFEST_HELPER, repo / "scripts" / "release_manifest.py")
    manifest = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    for entry in manifest["packages"]:
        package_dir = repo / str(entry["path"])
        package_dir.mkdir(parents=True)
        (package_dir / "pyproject.toml").write_text(
            f"[project]\nname = \"{entry['name']}\"\nversion = \"0.0.1\"\n",
            encoding="utf-8",
        )
        changelog_path = repo / str(entry["changelog"])
        changelog_path.write_text(f"# {entry['name']}\n", encoding="utf-8")
    (repo / "config" / "release" / "packages.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return repo


def test_validate_detects_unlisted_pyproject_directory(tmp_path: Path) -> None:
    repo = _build_drift_fixture(tmp_path)
    stray = repo / "packages" / "stray-package"
    stray.mkdir()
    (stray / "pyproject.toml").write_text(
        "[project]\nname = \"stray-package\"\nversion = \"0.0.1\"\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "release_manifest.py"), "validate"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "stray-package" in result.stderr


def test_validate_detects_publish_workflow_choice_drift(tmp_path: Path) -> None:
    repo = _build_drift_fixture(tmp_path)
    workflow_dir = repo / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    workflow_path = workflow_dir / "release-publish.yml"
    workflow_path.write_text(
        "on:\n"
        "  workflow_dispatch:\n"
        "    inputs:\n"
        "      package:\n"
        "        type: choice\n"
        "        options:\n"
        "          - workstate-protocol\n"
        "          - mcp-workstate-handoff\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "release_manifest.py"), "validate"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "release-publish.yml" in result.stderr


def test_validate_passes_from_subdirectory_cwd(tmp_path: Path) -> None:
    repo = _build_drift_fixture(tmp_path)
    subdir = repo / "packages" / "workstate-protocol"

    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "release_manifest.py"), "validate"],
        cwd=subdir,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr