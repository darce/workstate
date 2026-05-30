"""Hermetic, dry-run-only tests for the public-release orchestrator.

These tests never perform a network mutation. They build a fixture repo that
copies the real release tooling, stub ``git``/``uvx`` on PATH so the embedded
``scripts/release.sh plan --json`` call runs offline, and drive
``scripts/release_public.py`` exactly as an operator would. The mutating
``--execute`` path (git push / tag push / PyPI upload) is asserted to be gated
behind both ``--execute`` and an interactive confirmation, and is never
exercised.

Style mirrors test_pending_recovery.py (fixture repo + PATH-stubbed CLIs +
subprocess invocation + assertions on combined output / JSON).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SOURCE_RELEASE_PUBLIC = REPO_ROOT / "scripts" / "release_public.py"
SOURCE_RELEASE_SCRIPT = REPO_ROOT / "scripts" / "release.sh"
SOURCE_RELEASE_MANIFEST = REPO_ROOT / "config" / "release" / "packages.json"
SOURCE_RELEASE_MANIFEST_HELPER = REPO_ROOT / "scripts" / "release_manifest.py"
SOURCE_EXPORT_PUBLIC = REPO_ROOT / "scripts" / "export_public.py"

# publish=true packages (mcp-workstate-canvas is publish=false and excluded).
PUBLISHABLE_PACKAGES = (
    "workstate-protocol",
    "mcp-workstate-handoff",
    "mcp-workstate-orchestrator",
    "workstate-bootstrap",
    "workstate-codex-bridge",
)
ALL_PACKAGES = PUBLISHABLE_PACKAGES + ("mcp-workstate-canvas",)


def _build_fixture(tmp_path: Path, *, version: str = "1.2.3") -> Path:
    repo = tmp_path / "repo"
    (repo / "config" / "release").mkdir(parents=True)
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(SOURCE_RELEASE_PUBLIC, repo / "scripts" / "release_public.py")
    shutil.copy2(SOURCE_RELEASE_SCRIPT, repo / "scripts" / "release.sh")
    shutil.copy2(SOURCE_RELEASE_MANIFEST, repo / "config" / "release" / "packages.json")
    shutil.copy2(SOURCE_RELEASE_MANIFEST_HELPER, repo / "scripts" / "release_manifest.py")
    shutil.copy2(SOURCE_EXPORT_PUBLIC, repo / "scripts" / "export_public.py")
    (repo / "scripts" / "release.sh").chmod(0o755)
    (repo / "scripts" / "release_manifest.py").chmod(0o755)
    (repo / "scripts" / "release_public.py").chmod(0o755)

    for package in ALL_PACKAGES:
        package_dir = repo / "packages" / package
        package_dir.mkdir(parents=True)
        (package_dir / "pyproject.toml").write_text(
            f'[project]\nname = "{package}"\nversion = "{version}"\n',
            encoding="utf-8",
        )
        (package_dir / "CHANGELOG.md").write_text(
            f"# {package}\n\n## {version}\n\n- Fixture entry.\n",
            encoding="utf-8",
        )
    return repo


def _install_offline_cli(tmp_path: Path, *, version: str = "1.2.3") -> Path:
    """Stub ``git``/``uvx`` so ``release.sh plan --json`` runs offline.

    workstate-protocol is reported as already released; every other
    publishable package is pending_upload. No stub ever performs a real
    network call or mutation.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    git_stub = bin_dir / "git"
    git_stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'version="{version}"\n'
        'if [[ ${1:-} == "rev-parse" && ${2:-} == "-q" && ${3:-} == "--verify" ]]; then\n'
        "    tag=${4#refs/tags/}\n"
        f'    if [[ $tag == workstate-protocol-v{version} ]]; then\n'
        "        printf 'deadbeef\\n'; exit 0\n"
        "    fi\n"
        "    exit 1\n"
        "fi\n"
        'if [[ ${1:-} == "ls-remote" && ${2:-} == "--tags" ]]; then\n'
        "    tag=${4#refs/tags/}\n"
        f'    if [[ $tag == workstate-protocol-v{version} ]]; then\n'
        "        printf 'deadbeef\\trefs/tags/%s\\n' \"$tag\"; exit 0\n"
        "    fi\n"
        "    exit 0\n"
        "fi\n"
        'if [[ ${1:-} == "tag" && ${2:-} == "-l" ]]; then\n'
        "    printf 'v0.1.9\\n'; exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    git_stub.chmod(0o755)

    uvx_stub = bin_dir / "uvx"
    uvx_stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "pkg=${@: -1}\n"
        f'version="{version}"\n'
        'if [[ $pkg == workstate-protocol ]]; then\n'
        '    printf "Available versions: %s\\n" "$version"\n'
        "else\n"
        '    printf "%s\\n" "Available versions: 0.0.1"\n'
        "fi\n",
        encoding="utf-8",
    )
    uvx_stub.chmod(0o755)
    return bin_dir


def _env(bin_dir: Path, **extra: str) -> dict[str, str]:
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    env.update(extra)
    return env


def _run(repo: Path, env: dict[str, str], *args: str, stdin: str | None = None):
    return subprocess.run(
        [sys.executable, str(repo / "scripts" / "release_public.py"), *args],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        input=stdin,
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_dry_run_prints_pipeline_steps_in_order(tmp_path: Path) -> None:
    repo = _build_fixture(tmp_path)
    env = _env(_install_offline_cli(tmp_path))

    result = _run(repo, env)

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "mode: dry-run" in combined
    # export -> push -> tag-sync -> publish steps appear in order.
    order = ["export", "push", "tag-sync", "status"]
    positions = [result.stdout.index(step) for step in order]
    assert positions == sorted(positions), combined
    # Mutating steps are planned, not executed, in dry-run.
    assert "no network mutation performed" in combined


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_dry_run_reports_five_state_status(tmp_path: Path) -> None:
    repo = _build_fixture(tmp_path)
    env = _env(_install_offline_cli(tmp_path))

    result = _run(repo, env, "--json")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["mode"] == "dry-run"
    # Each status row distinguishes the five release states.
    for entry in report["status"]:
        assert set(entry) >= {
            "private_source_tag",
            "public_export_branch",
            "public_tag",
            "pypi_publication",
            "trusted_publisher",
        }
    states = {e["name"]: e for e in report["status"]}
    assert states["workstate-protocol"]["pypi_publication"] == "published"
    assert states["mcp-workstate-handoff"]["pypi_publication"] == "unpublished"
    # Dry-run never probes PyPI for the publisher state.
    assert all(
        e["trusted_publisher"] == "unchecked (dry-run)"
        for e in report["status"]
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_preflight_enumerates_exactly_publishable_packages(tmp_path: Path) -> None:
    repo = _build_fixture(tmp_path)
    env = _env(_install_offline_cli(tmp_path))

    result = _run(repo, env, "--json")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    would_check = report["preflight"]["would_check"]
    # Exactly the publish=true set — mcp-workstate-canvas (publish=false) excluded.
    assert would_check == list(PUBLISHABLE_PACKAGES)
    assert "mcp-workstate-canvas" not in would_check
    assert report["preflight"]["checked"] is False
    assert report["preflight"]["binding"] == {
        "owner": "darce",
        "repository": "workstate",
        "workflow": "release-publish.yml",
        "environment": "pypi",
    }


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_preflight_fails_when_a_package_lacks_a_publisher(tmp_path: Path) -> None:
    repo = _build_fixture(tmp_path)
    # Cover all but one publishable package: the preflight must fail clearly.
    covered = ",".join(PUBLISHABLE_PACKAGES[:-1])
    env = _env(
        _install_offline_cli(tmp_path),
        RELEASE_PUBLIC_FAKE_PUBLISHERS=covered,
    )

    result = _run(repo, env, "--execute", "--assume-yes", "--preflight-only")

    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    missing = PUBLISHABLE_PACKAGES[-1]
    assert missing in combined
    assert "missing Trusted Publisher" in combined
    # No mutating step ran.
    assert "About to PUSH" not in combined


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_preflight_passes_when_all_publishers_present(tmp_path: Path) -> None:
    repo = _build_fixture(tmp_path)
    covered = ",".join(PUBLISHABLE_PACKAGES)
    env = _env(
        _install_offline_cli(tmp_path),
        RELEASE_PUBLIC_FAKE_PUBLISHERS=covered,
    )

    # --preflight-only stops before any mutating step even when covered.
    result = _run(repo, env, "--execute", "--assume-yes", "--preflight-only", "--json")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["preflight"]["checked"] is True
    assert report["preflight"]["ok"] is True
    assert report["preflight"]["missing"] == []
    assert all(
        e["trusted_publisher"] == "ready" for e in report["status"]
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_execute_without_confirmation_does_not_mutate(tmp_path: Path) -> None:
    repo = _build_fixture(tmp_path)
    covered = ",".join(PUBLISHABLE_PACKAGES)
    env = _env(
        _install_offline_cli(tmp_path),
        RELEASE_PUBLIC_FAKE_PUBLISHERS=covered,
    )

    # Decline the interactive prompt (wrong answer => no mutation).
    result = _run(repo, env, "--execute", stdin="no\n")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "not confirmed" in combined
    assert "no network-mutating step ran" in combined
    report_will_mutate = _run(repo, env, "--execute", "--json", stdin="no\n")
    payload = json.loads(report_will_mutate.stdout)
    assert payload["confirmed"] is False
    assert payload["will_mutate"] is False


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_execute_with_confirmation_is_gated_build_only(tmp_path: Path) -> None:
    """Confirmed --execute reaches the operator-gated mutation path, which is
    intentionally not enabled in this build-only slice (implementation note D3)."""
    repo = _build_fixture(tmp_path)
    covered = ",".join(PUBLISHABLE_PACKAGES)
    env = _env(
        _install_offline_cli(tmp_path),
        RELEASE_PUBLIC_FAKE_PUBLISHERS=covered,
    )

    result = _run(repo, env, "--execute", "--assume-yes")

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "build-only" in combined
    assert "operator-gated" in combined
