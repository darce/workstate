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
    "workstate-system",
    "workstate-stack",
)
ALL_PACKAGES = PUBLISHABLE_PACKAGES + ("mcp-workstate-canvas",)

# Direct import of the script module for unit-testing its pure helpers (the rest
# of the suite drives it as a subprocess). release_public has no import-time side
# effects, so loading it straight from its path is safe.
import importlib.util as _importlib_util  # noqa: WORKSTATE-REF-402

_spec = _importlib_util.spec_from_file_location("release_public", SOURCE_RELEASE_PUBLIC)
release_public = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(release_public)


def _build_fixture(tmp_path: Path, *, version: str = "1.2.3") -> Path:
    repo = tmp_path / "repo"
    (repo / "config" / "release").mkdir(parents=True)
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(SOURCE_RELEASE_PUBLIC, repo / "scripts" / "release_public.py")
    shutil.copy2(SOURCE_RELEASE_SCRIPT, repo / "scripts" / "release.sh")
    shutil.copy2(SOURCE_RELEASE_MANIFEST, repo / "config" / "release" / "packages.json")
    shutil.copy2(
        SOURCE_RELEASE_MANIFEST_HELPER, repo / "scripts" / "release_manifest.py"
    )
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
        # _execute is the only caller that uses the `git -C <dir>` form; the plan
        # computation never does. Log + short-circuit those so the mutating
        # pipeline runs offline and its command order can be asserted.
        'if [[ ${1:-} == "-C" ]]; then\n'
        '    if [[ -n "${RELEASE_PUBLIC_TEST_LOG:-}" ]]; then\n'
        '        echo "GIT $*" >> "$RELEASE_PUBLIC_TEST_LOG"\n'
        "    fi\n"
        "    exit 0\n"
        "fi\n"
        'if [[ ${1:-} == "rev-parse" && ${2:-} == "-q" && ${3:-} == "--verify" ]]; then\n'
        "    tag=${4#refs/tags/}\n"
        f"    if [[ $tag == workstate-protocol-v{version} ]]; then\n"
        "        printf 'deadbeef\\n'; exit 0\n"
        "    fi\n"
        "    exit 1\n"
        "fi\n"
        'if [[ ${1:-} == "ls-remote" && ${2:-} == "--tags" ]]; then\n'
        "    tag=${4#refs/tags/}\n"
        f"    if [[ $tag == workstate-protocol-v{version} ]]; then\n"
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
        "if [[ $pkg == workstate-protocol ]]; then\n"
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
        e["trusted_publisher"] == "unchecked (dry-run)" for e in report["status"]
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

    result = _run(
        repo, env, "--execute", "--assume-yes", "--preflight-only", "--probe-publishers"
    )

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
    result = _run(
        repo,
        env,
        "--execute",
        "--assume-yes",
        "--preflight-only",
        "--probe-publishers",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["preflight"]["checked"] is True
    assert report["preflight"]["ok"] is True
    assert report["preflight"]["missing"] == []
    assert all(e["trusted_publisher"] == "ready" for e in report["status"])


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


def _install_export_stub(tmp_path: Path) -> Path:
    """An export stub that materializes the --out dir without git/network.

    Pointed at via RELEASE_PUBLIC_EXPORT_CMD so _execute's mutating pipeline
    runs hermetically; it appends an EXPORT marker to RELEASE_PUBLIC_TEST_LOG
    so the export-before-push ordering can be asserted.
    """
    stub = tmp_path / "export_stub.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'out=""\n'
        "while [[ $# -gt 0 ]]; do\n"
        '    case "$1" in\n'
        '        --out) out="$2"; shift 2;;\n'
        "        *) shift;;\n"
        "    esac\n"
        "done\n"
        'mkdir -p "$out"\n'
        'if [[ -n "${RELEASE_PUBLIC_TEST_LOG:-}" ]]; then\n'
        '    echo "EXPORT --out $out" >> "$RELEASE_PUBLIC_TEST_LOG"\n'
        "fi\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_publishers_verified_passes_preflight_without_probing(tmp_path: Path) -> None:
    """--publishers-verified is the real operator-confirm path: it passes the
    preflight without the test env seam and without touching PyPI."""
    repo = _build_fixture(tmp_path)
    # No RELEASE_PUBLIC_FAKE_PUBLISHERS and no network: the flag alone covers it.
    env = _env(_install_offline_cli(tmp_path))

    result = _run(
        repo,
        env,
        "--execute",
        "--assume-yes",
        "--preflight-only",
        "--publishers-verified",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["preflight"]["checked"] is True
    assert report["preflight"]["ok"] is True
    assert report["preflight"]["operator_verified"] is True
    assert report["preflight"]["missing"] == []
    assert all(e["trusted_publisher"] == "ready" for e in report["status"])


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_default_assumes_verified_publishers_without_probing(tmp_path: Path) -> None:
    """Verified publishers are the DEFAULT: with no publisher flag at all the
    preflight passes as operator_verified without probing PyPI (the bindings
    were confirmed once in the settings UI; --probe-publishers opts back in)."""
    repo = _build_fixture(tmp_path)
    # No RELEASE_PUBLIC_FAKE_* seam and no network: the default alone covers it.
    env = _env(_install_offline_cli(tmp_path))

    result = _run(repo, env, "--execute", "--assume-yes", "--preflight-only", "--json")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["preflight"]["checked"] is True
    assert report["preflight"]["ok"] is True
    assert report["preflight"]["operator_verified"] is True
    assert report["preflight"]["unverifiable"] == []
    assert report["preflight"]["missing"] == []


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_execute_runs_export_push_tagsync_in_order(tmp_path: Path) -> None:
    """A confirmed, publisher-verified --execute runs export -> push ->
    tag-sync in order, against the test remote, with no real network call."""
    repo = _build_fixture(tmp_path, version="1.2.3")
    log = tmp_path / "calls.log"
    env = _env(
        _install_offline_cli(tmp_path, version="1.2.3"),
        RELEASE_PUBLIC_EXPORT_CMD=f"bash {_install_export_stub(tmp_path)}",
        RELEASE_PUBLIC_REMOTE="test-remote",
        RELEASE_PUBLIC_TEST_LOG=str(log),
    )

    result = _run(repo, env, "--execute", "--assume-yes", "--publishers-verified")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    lines = log.read_text().splitlines()
    # export precedes the main push, which precedes the tag-sync push.
    export_idx = next(i for i, ln in enumerate(lines) if ln.startswith("EXPORT"))
    main_push_idx = next(
        i
        for i, ln in enumerate(lines)
        if "push --force test-remote HEAD:refs/heads/main" in ln
    )
    assert export_idx < main_push_idx, lines
    # Per-package tag family + the monorepo consumer tag are created then pushed.
    assert any("tag -f workstate-protocol-v1.2.3" in ln for ln in lines), lines
    assert any("tag -f workstate-codex-bridge-v1.2.3" in ln for ln in lines), lines
    tag_push = next(
        (
            i
            for i, ln in enumerate(lines)
            if "push --force test-remote" in ln and "-v1.2.3" in ln
        ),
        None,
    )
    assert tag_push is not None and tag_push > main_push_idx, lines
    # The monorepo tag (vX.Y.Z, computed from the fixture's v0.1.9) is in the
    # pushed tag set.
    assert any(
        "push --force test-remote" in ln and " v0." in f" {ln} " for ln in lines
    ), lines


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_execute_cleans_up_export_temp_dir(tmp_path: Path) -> None:
    """_execute removes its throwaway export tree (in a finally), so repeated
    --execute runs do not accumulate workstate-public-export-* dirs in TMPDIR."""
    repo = _build_fixture(tmp_path, version="1.2.3")
    tmpdir = tmp_path / "exptmp"
    tmpdir.mkdir()
    env = _env(
        _install_offline_cli(tmp_path, version="1.2.3"),
        RELEASE_PUBLIC_EXPORT_CMD=f"bash {_install_export_stub(tmp_path)}",
        RELEASE_PUBLIC_REMOTE="test-remote",
        TMPDIR=str(tmpdir),
    )

    result = _run(repo, env, "--execute", "--assume-yes", "--publishers-verified")

    assert result.returncode == 0, result.stdout + result.stderr
    leftover = list(tmpdir.glob("workstate-public-export-*"))
    assert leftover == [], f"export temp dir not cleaned up: {leftover}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_execute_is_idempotent(tmp_path: Path) -> None:
    """Re-running the confirmed pipeline converges (force-push semantics)."""
    repo = _build_fixture(tmp_path)
    env = _env(
        _install_offline_cli(tmp_path),
        RELEASE_PUBLIC_EXPORT_CMD=f"bash {_install_export_stub(tmp_path)}",
        RELEASE_PUBLIC_REMOTE="test-remote",
    )

    first = _run(repo, env, "--execute", "--assume-yes", "--publishers-verified")
    second = _run(repo, env, "--execute", "--assume-yes", "--publishers-verified")

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_execute_still_gated_without_confirmation(tmp_path: Path) -> None:
    """Even publisher-verified, --execute without confirmation mutates nothing."""
    repo = _build_fixture(tmp_path)
    log = tmp_path / "calls.log"
    env = _env(
        _install_offline_cli(tmp_path),
        RELEASE_PUBLIC_EXPORT_CMD=f"bash {_install_export_stub(tmp_path)}",
        RELEASE_PUBLIC_REMOTE="test-remote",
        RELEASE_PUBLIC_TEST_LOG=str(log),
    )

    # Decline the interactive prompt; no export/push/tag must run.
    result = _run(repo, env, "--execute", "--publishers-verified", stdin="no\n")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "no network-mutating step ran" in combined
    assert not log.exists() or log.read_text() == ""


# --- S4a: publisher preflight reports `unverifiable` vs `missing` -------------


def test_classify_publisher_payload_distinguishes_unverifiable_from_missing() -> None:
    """The classifier is the tri-state core: 404 / explicit-empty == missing,
    an exposed matching binding == covered, and a 200 with no publisher metadata
    == unverifiable (the PyPI JSON API hides it for most projects)."""
    classify = release_public.classify_publisher_payload
    binding_match = {
        "owner": "darce",
        "repository": "workstate",
        "workflow": "release-publish.yml",
    }
    # 404 — the project does not exist yet, so a publisher is genuinely absent.
    assert classify(None) == "missing"
    # 200 but the public JSON API exposes no publisher field at all.
    assert classify({"info": {"name": "x"}}) == "unverifiable"
    # An explicit, empty publisher list IS proof of absence.
    assert classify({"trusted-publishers": []}) == "missing"
    # An explicit matching publisher is the only "covered" signal.
    assert classify({"trusted-publishers": [binding_match]}) == "covered"
    # A non-matching publisher is still not our binding -> missing.
    assert classify({"trusted-publishers": [{"owner": "someone-else"}]}) == "missing"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_preflight_reports_unverifiable_distinct_from_missing(tmp_path: Path) -> None:
    """A live probe that finds the project but no exposed publisher metadata
    reports `unverifiable` (confirm in the PyPI UI + --publishers-verified), NOT
    a flat `missing` binding — that mislabel was the 0019 footgun."""
    repo = _build_fixture(tmp_path)
    # Every publishable package exists on PyPI but exposes no publisher metadata.
    env = _env(
        _install_offline_cli(tmp_path),
        RELEASE_PUBLIC_FAKE_UNVERIFIABLE=",".join(PUBLISHABLE_PACKAGES),
    )

    result = _run(
        repo,
        env,
        "--execute",
        "--assume-yes",
        "--preflight-only",
        "--probe-publishers",
        "--json",
    )

    combined = result.stdout + result.stderr
    # Blocked (cannot auto-verify) but NOT framed as a missing-binding error.
    assert result.returncode == 1, combined
    report = json.loads(result.stdout)
    assert report["preflight"]["checked"] is True
    assert report["preflight"]["missing"] == []
    assert set(report["preflight"]["unverifiable"]) == set(PUBLISHABLE_PACKAGES)
    assert report["preflight"]["ok"] is False
    # Operator guidance points at the escape hatch, not a config-error mislabel.
    assert "unverifiable" in combined
    assert "--publishers-verified" in combined
    assert "missing Trusted Publisher" not in combined
    # Status rows distinguish unverifiable from missing/ready.
    assert all(e["trusted_publisher"] == "unverifiable" for e in report["status"])


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_preflight_publishers_verified_clears_unverifiable(tmp_path: Path) -> None:
    """--publishers-verified is exactly the documented recovery for unverifiable:
    once the operator confirms out-of-band the preflight passes without probing."""
    repo = _build_fixture(tmp_path)
    env = _env(
        _install_offline_cli(tmp_path),
        RELEASE_PUBLIC_FAKE_UNVERIFIABLE=",".join(PUBLISHABLE_PACKAGES),
    )

    result = _run(
        repo,
        env,
        "--execute",
        "--assume-yes",
        "--preflight-only",
        "--publishers-verified",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["preflight"]["ok"] is True
    assert report["preflight"]["operator_verified"] is True
    assert report["preflight"]["unverifiable"] == []
    assert report["preflight"]["missing"] == []
