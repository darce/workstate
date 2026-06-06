"""implementation note S1 — `check_release_version_drift` gate.

Reproduces the 0019 ship bug (workstate-bootstrap/workstate-system shipped two
feature arcs with NO version bump, so their PyPI versions silently fell behind
HEAD) as a regression, and pins the gate's contract:

* For each publishable package, if any file under its *shipped payload* changed
  since the commit that set its current pyproject version, the gate fails until
  the version is bumped.
* The "shipped payload" set for workstate-system is derived from the single
  existing source — the ``force-include`` map — so the gate and the wheel
  cannot disagree before implementation note S3 co-locates the payload.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SOURCE = REPO_ROOT / "scripts" / "check_release_version_drift.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_release_version_drift_under_test", SOURCE
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_repo(tmp_path: Path) -> Path:
    """A synthetic monorepo with one publishable src-layout package at v0.1.0."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    _write(
        repo / "config" / "release" / "packages.json",
        json.dumps(
            {"packages": [{"name": "foo", "path": "packages/foo", "publish": True}]}
        ),
    )
    _write(
        repo / "packages" / "foo" / "pyproject.toml",
        '[project]\nname = "foo"\nversion = "0.1.0"\n',
    )
    _write(repo / "packages" / "foo" / "src" / "foo" / "__init__.py", "X = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed foo 0.1.0")
    return repo


# ---------------------------------------------------------------------------
# payload-set derivation (resolves pa0020-s1: gate set == force-include set)
# ---------------------------------------------------------------------------


def test_workstate_system_payload_is_union_of_shipped_surfaces() -> None:
    import tomllib

    mod = _load_module()
    wheel = tomllib.loads(
        (REPO_ROOT / "packages" / "workstate-system" / "pyproject.toml").read_text()
    )["tool"]["hatch"]["build"]["targets"]["wheel"]
    force_include = set(wheel.get("force-include", {}).keys())
    only_include = set(wheel.get("only-include", []))
    entry = {
        "name": "workstate-system",
        "path": "packages/workstate-system",
        "publish": True,
    }
    payload = set(mod.payload_paths(REPO_ROOT, entry))
    # Union of every shipped surface — force-include keys AND the only-include
    # importable namespace (resolves revA-only-include-namespace-not-covered).
    assert payload == {
        f"packages/workstate-system/{k}" for k in (force_include | only_include)
    }
    assert "packages/workstate-system/workstate_system" in payload


# ---------------------------------------------------------------------------
# git-history drift detection (the 0019 silent-drift reproduction)
# ---------------------------------------------------------------------------


def test_clean_tree_has_no_drift(tmp_path: Path) -> None:
    mod = _load_module()
    repo = _make_repo(tmp_path)
    assert mod.check(repo) == []


def test_payload_change_without_bump_is_drift(tmp_path: Path) -> None:
    mod = _load_module()
    repo = _make_repo(tmp_path)
    # Change shipped payload, commit, but DO NOT bump the version (the 0019 bug).
    _write(
        repo / "packages" / "foo" / "src" / "foo" / "__init__.py", "X = 2  # feature\n"
    )
    _git(repo, "commit", "-am", "feat: change foo payload without a bump")
    drift = mod.check(repo)
    assert [d.name for d in drift] == ["foo"]
    assert "packages/foo/src/foo/__init__.py" in drift[0].changed_files


def test_bumping_the_version_clears_drift(tmp_path: Path) -> None:
    mod = _load_module()
    repo = _make_repo(tmp_path)
    _write(
        repo / "packages" / "foo" / "src" / "foo" / "__init__.py", "X = 2  # feature\n"
    )
    _git(repo, "commit", "-am", "feat: change foo payload")
    _write(
        repo / "packages" / "foo" / "pyproject.toml",
        '[project]\nname = "foo"\nversion = "0.1.1"\n',
    )
    _git(repo, "commit", "-am", "release: foo 0.1.1")
    assert mod.check(repo) == []


def test_dependency_pin_rewrite_without_bump_is_drift(tmp_path: Path) -> None:
    """Wheel METADATA is payload: a meta-package pin rewrite must force a bump.

    Reproduces revD-stack-pins-invisible-to-gate: ``workstate-stack``'s entire
    payload is its ``[project.dependencies]`` exact pins, which no payload
    *file* covers — a stack-pins-sync commit without a version bump escaped
    the gate.
    """
    mod = _load_module()
    repo = _make_repo(tmp_path)
    _write(
        repo / "packages" / "foo" / "pyproject.toml",
        '[project]\nname = "foo"\nversion = "0.1.0"\ndependencies = ["bar==2.0.0"]\n',
    )
    _git(repo, "commit", "-am", "chore: rewrite pins without a bump")
    drift = mod.check(repo)
    assert [d.name for d in drift] == ["foo"]
    assert "packages/foo/pyproject.toml" in drift[0].changed_files


def test_pin_rewrite_with_bump_clears_drift(tmp_path: Path) -> None:
    mod = _load_module()
    repo = _make_repo(tmp_path)
    _write(
        repo / "packages" / "foo" / "pyproject.toml",
        '[project]\nname = "foo"\nversion = "0.1.1"\ndependencies = ["bar==2.0.0"]\n',
    )
    _git(repo, "commit", "-am", "release: foo 0.1.1 with new pins")
    assert mod.check(repo) == []


def test_tool_section_only_change_is_not_drift(tmp_path: Path) -> None:
    """Non-[project] pyproject edits (tool config) ship nothing — no false drift."""
    mod = _load_module()
    repo = _make_repo(tmp_path)
    _write(
        repo / "packages" / "foo" / "pyproject.toml",
        '[project]\nname = "foo"\nversion = "0.1.0"\n\n[tool.ruff]\nline-length = 100\n',
    )
    _git(repo, "commit", "-am", "chore: tool config only")
    assert mod.check(repo) == []


def test_cli_exits_1_on_drift(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo / "packages" / "foo" / "src" / "foo" / "__init__.py", "X = 2\n")
    _git(repo, "commit", "-am", "feat: drift")
    proc = subprocess.run(
        [sys.executable, str(SOURCE), "--repo-root", str(repo)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "foo" in proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# Remediation regressions (S1 parallel review)
# ---------------------------------------------------------------------------


def test_drift_detected_with_non_canonical_version_spelling(tmp_path: Path) -> None:
    """The version-set commit is found by tomllib-parsing each revision, not a
    source-text pickaxe — so a no-spaces / single-quoted spelling still detects
    drift (revA-version-set-commit-spelling-silent-skip)."""
    mod = _load_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    _write(
        repo / "config" / "release" / "packages.json",
        json.dumps(
            {"packages": [{"name": "foo", "path": "packages/foo", "publish": True}]}
        ),
    )
    # Non-canonical spelling: no spaces around '=' (a common formatter output).
    _write(
        repo / "packages" / "foo" / "pyproject.toml",
        '[project]\nname="foo"\nversion="0.1.0"\n',
    )
    _write(repo / "packages" / "foo" / "src" / "foo" / "__init__.py", "X = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed foo 0.1.0 (no-space spelling)")
    _write(repo / "packages" / "foo" / "src" / "foo" / "__init__.py", "X = 2\n")
    _git(repo, "commit", "-am", "feat: payload change, no bump")
    drift = mod.check(repo)
    assert [d.name for d in drift] == ["foo"]
    assert "packages/foo/src/foo/__init__.py" in drift[0].changed_files


def test_uncommitted_version_is_hard_failure(tmp_path: Path) -> None:
    """A current version not in git history (uncommitted bump) is a hard failure,
    not a silent skip (revA-skip-on-none-fail-quiet)."""
    mod = _load_module()
    repo = _make_repo(tmp_path)
    # Bump the version in the WORKING TREE only — never committed.
    _write(
        repo / "packages" / "foo" / "pyproject.toml",
        '[project]\nname = "foo"\nversion = "0.2.0"\n',
    )
    drift = mod.check(repo)
    assert [d.name for d in drift] == ["foo"]
    assert "not present in git history" in drift[0].reason


def test_pure_rename_of_pyproject_after_set_keeps_baseline(tmp_path: Path) -> None:
    """A pure-rename commit that moves pyproject.toml (no version/payload change)
    must not be mis-read as the version-set commit. `--follow` traverses the
    rename so the baseline stays at the real set commit (revC-pure-rename-after-set)."""
    mod = _load_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    _write(
        repo / "config" / "release" / "packages.json",
        json.dumps(
            {"packages": [{"name": "foo", "path": "packages/foo", "publish": True}]}
        ),
    )
    # Version 0.1.0 set under an OLD path, with payload.
    _write(
        repo / "old" / "foo" / "pyproject.toml",
        '[project]\nname = "foo"\nversion = "0.1.0"\n',
    )
    _write(repo / "old" / "foo" / "src" / "foo" / "__init__.py", "X = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed foo 0.1.0 at old path")
    # Pure rename to the current path — no version or payload change.
    (repo / "packages").mkdir(parents=True, exist_ok=True)
    _git(repo, "mv", "old/foo", "packages/foo")
    _git(repo, "commit", "-m", "chore: relocate foo (pure rename)")
    # No payload change since the version was set → clean, not drift.
    assert mod.check(repo) == []
    # And a real post-rename payload change without a bump IS drift.
    _write(repo / "packages" / "foo" / "src" / "foo" / "__init__.py", "X = 2\n")
    _git(repo, "commit", "-am", "feat: payload change after rename, no bump")
    drift = mod.check(repo)
    assert [d.name for d in drift] == ["foo"]


def test_shallow_clone_is_hard_failure(tmp_path: Path) -> None:
    """On a shallow clone the git-log walk cannot see history, so the gate must
    refuse loudly (exit 2) instead of silently passing (revD-ci-gate-shallow)."""
    origin = _make_repo(tmp_path)
    # A genuine drift exists in origin so a false pass would be observable.
    _write(origin / "packages" / "foo" / "src" / "foo" / "__init__.py", "X = 2\n")
    _git(origin, "commit", "-am", "feat: drift")
    shallow = tmp_path / "shallow"
    _git(
        tmp_path,
        "clone",
        "--depth",
        "1",
        f"file://{origin}",
        str(shallow),
    )
    proc = subprocess.run(
        [sys.executable, str(SOURCE), "--repo-root", str(shallow)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "shallow" in proc.stdout + proc.stderr


def test_multi_package_reports_only_the_drifted_one(tmp_path: Path) -> None:
    """With two publishable packages, only the one whose payload changed without a
    bump is reported (revB-test-gaps; exercises the per-package loop)."""
    mod = _load_module()
    repo = _make_repo(tmp_path)  # package 'foo' at 0.1.0, clean
    _write(
        repo / "config" / "release" / "packages.json",
        json.dumps(
            {
                "packages": [
                    {"name": "foo", "path": "packages/foo", "publish": True},
                    {"name": "bar", "path": "packages/bar", "publish": True},
                ]
            }
        ),
    )
    _write(
        repo / "packages" / "bar" / "pyproject.toml",
        '[project]\nname = "bar"\nversion = "0.1.0"\n',
    )
    _write(repo / "packages" / "bar" / "src" / "bar" / "__init__.py", "Y = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add bar 0.1.0")
    # Drift ONLY foo.
    _write(repo / "packages" / "foo" / "src" / "foo" / "__init__.py", "X = 2\n")
    _git(repo, "commit", "-am", "feat: foo payload change, no bump")
    drift = mod.check(repo)
    assert [d.name for d in drift] == ["foo"]
