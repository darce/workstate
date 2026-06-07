"""Tests for the pypi_without_tag byte-parity reconciliation path (implementation note S4c).

The reconciler refuses to create a version tag unless the candidate commit's
package source is byte-identical to the published PyPI sdist. These tests build
a real throwaway git repo and real ``.tar.gz`` sdists in tmp so the comparison
is exercised against genuine git blobs and tar members, never the network.
"""

from __future__ import annotations

import importlib.util as _importlib_util
import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RECONCILE_SOURCE = REPO_ROOT / "scripts" / "release_reconcile_tag.py"

_spec = _importlib_util.spec_from_file_location(
    "release_reconcile_tag", RECONCILE_SOURCE
)
reconcile_mod = _importlib_util.module_from_spec(_spec)
# Register before exec so @dataclass can resolve the module via sys.modules.
sys.modules["release_reconcile_tag"] = reconcile_mod
_spec.loader.exec_module(reconcile_mod)

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed"
)


def _make_sdist(
    tmp_path: Path,
    *,
    name: str,
    version: str,
    files: dict[str, bytes],
    include_pkg_info: bool = True,
) -> Path:
    """Build a ``<name>-<version>.tar.gz`` with the given ``relpath -> bytes`` source."""
    out = tmp_path / f"{name}-{version}.tar.gz"
    root = f"{name}-{version}"
    with tarfile.open(out, "w:gz") as tar:
        for relpath, data in files.items():
            info = tarfile.TarInfo(f"{root}/{relpath}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if include_pkg_info:
            meta = b"Metadata-Version: 2.1\nName: %s\n" % name.encode()
            info = tarfile.TarInfo(f"{root}/PKG-INFO")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
    return out


def _git_repo(tmp_path: Path, *, pkg_path: str, files: dict[str, bytes]) -> Path:
    """Init a repo and commit ``files`` under ``pkg_path`` (relpath -> bytes)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    for relpath, data in files.items():
        path = repo / pkg_path / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
    return repo


_SOURCE = {
    "pyproject.toml": b"[project]\nname = 'p'\nversion = '1.2.3'\n",
    "src/p/__init__.py": b"VALUE = 1\n",
}

# Same package, but its sdist declares an exclude glob so evals under a shipped
# root are NOT shipped — used to prove the reverse check honors declared excludes.
_SOURCE_WITH_EXCLUDE = {
    "pyproject.toml": (
        b"[project]\nname = 'p'\nversion = '1.2.3'\n\n"
        b"[tool.hatch.build.targets.sdist]\nexclude = ['**/evals/**']\n"
    ),
    "src/p/__init__.py": b"VALUE = 1\n",
}


def test_sdist_source_files_strips_prefix_and_skips_generated_metadata(
    tmp_path: Path,
) -> None:
    sdist = _make_sdist(tmp_path, name="p", version="1.2.3", files=_SOURCE)
    files = reconcile_mod.sdist_source_files(sdist.read_bytes())
    # The <name>-<version>/ prefix is stripped; PKG-INFO is excluded.
    assert set(files) == {"pyproject.toml", "src/p/__init__.py"}
    assert files["src/p/__init__.py"] == b"VALUE = 1\n"


@requires_git
def test_verify_parity_match_is_ok(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, pkg_path="packages/p", files=_SOURCE)
    sdist = _make_sdist(tmp_path, name="p", version="1.2.3", files=_SOURCE)
    report = reconcile_mod.verify_parity(
        reconcile_mod.sdist_source_files(sdist.read_bytes()),
        repo,
        "HEAD",
        "packages/p",
    )
    assert report.ok
    assert set(report.matched) == {"pyproject.toml", "src/p/__init__.py"}
    assert report.mismatched == []
    assert report.missing_in_commit == []


@requires_git
def test_verify_parity_mismatch_is_not_ok(tmp_path: Path) -> None:
    # The commit (HEAD) carries DRIFTED source vs what PyPI published.
    drifted = dict(_SOURCE)
    drifted["src/p/__init__.py"] = b"VALUE = 2  # HEAD drifted post-release\n"
    repo = _git_repo(tmp_path, pkg_path="packages/p", files=drifted)
    sdist = _make_sdist(tmp_path, name="p", version="1.2.3", files=_SOURCE)
    report = reconcile_mod.verify_parity(
        reconcile_mod.sdist_source_files(sdist.read_bytes()),
        repo,
        "HEAD",
        "packages/p",
    )
    assert not report.ok
    assert report.mismatched == ["src/p/__init__.py"]


@requires_git
def test_verify_parity_commit_extra_shipped_file_is_not_ok(tmp_path: Path) -> None:
    # The commit carries a NEW shipped-source file added post-release under the
    # same version; the published sdist never contained it. This is the 0019
    # hazard — the reverse check must catch it (bidirectional parity).
    plus = dict(_SOURCE)
    plus["src/p/new_feature.py"] = b"NEW = True\n"
    repo = _git_repo(tmp_path, pkg_path="packages/p", files=plus)
    sdist = _make_sdist(tmp_path, name="p", version="1.2.3", files=_SOURCE)
    report = reconcile_mod.verify_parity(
        reconcile_mod.sdist_source_files(sdist.read_bytes()),
        repo,
        "HEAD",
        "packages/p",
    )
    assert not report.ok
    assert report.extra_in_commit == ["src/p/new_feature.py"]
    assert report.mismatched == []
    assert report.missing_in_commit == []


@requires_git
def test_verify_parity_excluded_commit_extra_is_ignored(tmp_path: Path) -> None:
    # A commit file under a shipped root that the sdist `exclude` glob removes
    # (evals) must NOT trigger a false refusal — the reverse check honors the
    # package's declared sdist excludes.
    plus = dict(_SOURCE_WITH_EXCLUDE)
    plus["src/p/evals/bench.py"] = b"BENCH = 1\n"
    repo = _git_repo(tmp_path, pkg_path="packages/p", files=plus)
    sdist = _make_sdist(tmp_path, name="p", version="1.2.3", files=_SOURCE_WITH_EXCLUDE)
    report = reconcile_mod.verify_parity(
        reconcile_mod.sdist_source_files(sdist.read_bytes()),
        repo,
        "HEAD",
        "packages/p",
    )
    assert report.ok, (
        report.extra_in_commit,
        report.mismatched,
        report.missing_in_commit,
    )
    assert report.extra_in_commit == []


@requires_git
def test_verify_parity_missing_file_is_not_ok(tmp_path: Path) -> None:
    # The published sdist shipped a file the commit does not contain.
    repo = _git_repo(tmp_path, pkg_path="packages/p", files=_SOURCE)
    plus = dict(_SOURCE)
    plus["src/p/extra.py"] = b"EXTRA = True\n"
    sdist = _make_sdist(tmp_path, name="p", version="1.2.3", files=plus)
    report = reconcile_mod.verify_parity(
        reconcile_mod.sdist_source_files(sdist.read_bytes()),
        repo,
        "HEAD",
        "packages/p",
    )
    assert not report.ok
    assert report.missing_in_commit == ["src/p/extra.py"]


def _run_cli(repo: Path, sdist: Path, *args: str):
    import os

    env = {**os.environ, "RELEASE_RECONCILE_FAKE_SDIST": str(sdist)}
    return subprocess.run(
        [sys.executable, str(RECONCILE_SOURCE), "--repo", str(repo), *args],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
    )


@requires_git
def test_reconcile_cli_dry_run_emits_tag_command_on_match(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, pkg_path="packages/p", files=_SOURCE)
    sdist = _make_sdist(tmp_path, name="p", version="1.2.3", files=_SOURCE)
    result = _run_cli(
        repo, sdist, "--package", "p", "--version", "1.2.3", "--pkg-path", "packages/p"
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "would create tag: git tag p-v1.2.3" in combined
    # Dry-run created no tag.
    tags = subprocess.run(
        ["git", "tag", "-l"], cwd=repo, text=True, capture_output=True
    )
    assert tags.stdout.strip() == ""


@requires_git
def test_reconcile_cli_refuses_and_creates_no_tag_on_mismatch(tmp_path: Path) -> None:
    drifted = dict(_SOURCE)
    drifted["src/p/__init__.py"] = b"VALUE = 999\n"
    repo = _git_repo(tmp_path, pkg_path="packages/p", files=drifted)
    sdist = _make_sdist(tmp_path, name="p", version="1.2.3", files=_SOURCE)
    result = _run_cli(
        repo,
        sdist,
        "--package",
        "p",
        "--version",
        "1.2.3",
        "--pkg-path",
        "packages/p",
        "--execute",
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "REFUSING to tag" in combined
    assert "mismatch: src/p/__init__.py" in combined
    # Even with --execute, a mismatch must create no tag.
    tags = subprocess.run(
        ["git", "tag", "-l"], cwd=repo, text=True, capture_output=True
    )
    assert tags.stdout.strip() == ""


@requires_git
def test_reconcile_cli_refuses_on_unreleased_addition(tmp_path: Path) -> None:
    plus = dict(_SOURCE)
    plus["src/p/new_feature.py"] = b"NEW = True\n"
    repo = _git_repo(tmp_path, pkg_path="packages/p", files=plus)
    sdist = _make_sdist(tmp_path, name="p", version="1.2.3", files=_SOURCE)
    result = _run_cli(
        repo,
        sdist,
        "--package",
        "p",
        "--version",
        "1.2.3",
        "--pkg-path",
        "packages/p",
        "--execute",
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "unreleased addition in commit: src/p/new_feature.py" in combined
    tags = subprocess.run(
        ["git", "tag", "-l"], cwd=repo, text=True, capture_output=True
    )
    assert tags.stdout.strip() == ""


@requires_git
def test_reconcile_execute_creates_tag_on_match(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, pkg_path="packages/p", files=_SOURCE)
    sdist = _make_sdist(tmp_path, name="p", version="1.2.3", files=_SOURCE)
    result = _run_cli(
        repo,
        sdist,
        "--package",
        "p",
        "--version",
        "1.2.3",
        "--pkg-path",
        "packages/p",
        "--execute",
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    tags = subprocess.run(
        ["git", "tag", "-l"], cwd=repo, text=True, capture_output=True
    )
    assert tags.stdout.strip() == "p-v1.2.3"
