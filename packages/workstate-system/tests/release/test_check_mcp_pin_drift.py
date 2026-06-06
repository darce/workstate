"""Drift guard for the two hand-maintained managed-MCP-server pin sites.

Reproduces the v0.1.22 ship bug (orchestrator bumped 0.5.0 -> 0.5.1 while both
pin sites kept saying @0.5.0) as a regression and pins the guard's two entry
points: the ``release_prepare`` bump gate and the standalone steady-state
check.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
SOURCE_CHECK_PIN_DRIFT = REPO_ROOT / "scripts" / "check_mcp_pin_drift.py"
SOURCE_RELEASE_PREPARE = REPO_ROOT / "scripts" / "release_prepare.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_mcp_pin_drift_under_test", SOURCE_CHECK_PIN_DRIFT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass (under `from __future__ import
    # annotations`) can resolve the module via sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_repo(
    tmp_path: Path,
    *,
    orchestrator_pyproject: str = "0.5.1",
    handoff_pyproject: str = "0.12.0",
    orchestrator_install_pin: str = "0.5.1",
    orchestrator_yaml_pin: str = "0.5.1",
    orchestrator_doc_pin: str = "0.5.1",
    handoff_install_pin: str | None = None,
    handoff_yaml_pin: str | None = None,
    handoff_doc_pin: str | None = None,
    extra_install_line: str | None = None,
    omit_yaml_orchestrator: bool = False,
    omit_doc: bool = False,
    copy_scripts: bool = False,
) -> Path:
    """Build a synthetic monorepo with the two managed servers, three pin
    surfaces, and a non-server package. Per-surface orchestrator pins are
    parameterized so a test can introduce drift on exactly one surface."""
    repo = tmp_path / "repo"

    _write(
        repo / "config" / "release" / "packages.json",
        json.dumps(
            {
                "packages": [
                    {
                        "name": "workstate-protocol",
                        "path": "packages/workstate-protocol",
                        "distribution": "workstate-protocol",
                        "changelog": "packages/workstate-protocol/CHANGELOG.md",
                    },
                    {
                        "name": "mcp-workstate-handoff",
                        "path": "packages/mcp-workstate-handoff",
                        "distribution": "mcp-workstate-handoff",
                        "changelog": "packages/mcp-workstate-handoff/CHANGELOG.md",
                    },
                    {
                        "name": "mcp-workstate-orchestrator",
                        "path": "packages/mcp-workstate-orchestrator",
                        "distribution": "mcp-workstate-orchestrator",
                        "changelog": "packages/mcp-workstate-orchestrator/CHANGELOG.md",
                    },
                ]
            },
            indent=2,
        )
        + "\n",
    )

    for name, version in (
        ("workstate-protocol", "1.0.0"),
        ("mcp-workstate-handoff", handoff_pyproject),
        ("mcp-workstate-orchestrator", orchestrator_pyproject),
    ):
        _write(
            repo / "packages" / name / "pyproject.toml",
            f'[project]\nname = "{name}"\nversion = "{version}"\n',
        )
        _write(
            repo / "packages" / name / "CHANGELOG.md",
            "# Changelog\n\n## Unreleased\n",
        )

    # Per-surface handoff pins default to its pyproject version (aligned) unless
    # a test deliberately introduces handoff drift on one surface.
    h_install = (
        handoff_install_pin if handoff_install_pin is not None else handoff_pyproject
    )
    h_yaml = handoff_yaml_pin if handoff_yaml_pin is not None else handoff_pyproject
    h_doc = handoff_doc_pin if handoff_doc_pin is not None else handoff_pyproject

    # Pin site 1: DEFAULT_MCP_SERVERS in bootstrap install.py (text-scanned).
    # ``extra_install_line`` injects a stray <dist>@<ver> reference elsewhere in
    # the file (e.g. a comment) to exercise the conservative whole-file scan.
    _write(
        repo
        / "packages"
        / "workstate-bootstrap"
        / "src"
        / "workstate_bootstrap"
        / "install.py",
        "DEFAULT_MCP_SERVERS = {\n"
        '    "workstate-handoff-mcp": {\n'
        f'        "args": ["mcp-workstate-handoff@{h_install}", "serve-stdio"],\n'
        "    },\n"
        '    "workstate-orchestrator-mcp": {\n'
        f'        "args": ["mcp-workstate-orchestrator@{orchestrator_install_pin}", "serve-stdio"],\n'
        "    },\n"
        "}\n" + (f"{extra_install_line}\n" if extra_install_line else ""),
    )

    # Pin site 2: mcp_servers.yaml. ``omit_yaml_orchestrator`` drops the
    # orchestrator pin entirely (a deleted pin in a primary site is drift).
    yaml_text = (
        "mcp_servers:\n"
        "  - name: workstate-handoff-mcp\n"
        "    args:\n"
        f"      - mcp-workstate-handoff@{h_yaml}\n"
    )
    if not omit_yaml_orchestrator:
        yaml_text += (
            "  - name: workstate-orchestrator-mcp\n"
            "    args:\n"
            f"      - mcp-workstate-orchestrator@{orchestrator_yaml_pin}\n"
        )
    _write(
        repo
        / "packages"
        / "workstate-system"
        / "workstate_system"
        / "payload"
        / "config"
        / "agent-workflows"
        / "mcp_servers.yaml",
        yaml_text,
    )

    # Coupled surface: plugin-distribution.md. ``omit_doc`` removes it entirely
    # (an absent coupled doc is tolerated, unlike an absent primary site).
    if not omit_doc:
        _write(
            repo / "packages" / "workstate-system" / "docs" / "plugin-distribution.md",
            "# Plugin Distribution\n\n"
            f'"args": ["mcp-workstate-handoff@{h_doc}", "serve-stdio"]\n'
            f'"args": ["mcp-workstate-orchestrator@{orchestrator_doc_pin}", "serve-stdio"]\n',
        )

    if copy_scripts:
        _write(
            repo / "scripts" / "check_mcp_pin_drift.py",
            SOURCE_CHECK_PIN_DRIFT.read_text(encoding="utf-8"),
        )
        _write(
            repo / "scripts" / "release_prepare.py",
            SOURCE_RELEASE_PREPARE.read_text(encoding="utf-8"),
        )

    return repo


# --------------------------------------------------------------------------- #
# Standalone steady-state check (make check-mcp-pins)
# --------------------------------------------------------------------------- #


def test_aligned_pins_pass(tmp_path: Path) -> None:
    module = _load_module()
    repo = _build_repo(tmp_path)
    ok, messages = module.check_all(repo)
    assert ok, messages
    assert any("mcp-workstate-orchestrator pinned at 0.5.1" in m for m in messages)
    assert any("mcp-workstate-handoff pinned at 0.12.0" in m for m in messages)


def test_v0_1_22_regression_pyproject_ahead_of_all_pins(tmp_path: Path) -> None:
    """The exact shipped bug: orchestrator pyproject advanced to 0.5.1 but every
    pin still says 0.5.0. Steady-state check must fail and name all stale
    surfaces + the expected version."""
    module = _load_module()
    repo = _build_repo(
        tmp_path,
        orchestrator_pyproject="0.5.1",
        orchestrator_install_pin="0.5.0",
        orchestrator_yaml_pin="0.5.0",
        orchestrator_doc_pin="0.5.0",
    )
    ok, messages = module.check_all(repo)
    blob = "\n".join(messages)
    assert not ok
    assert "expected 0.5.1" in blob
    assert "DEFAULT_MCP_SERVERS" in blob
    assert "mcp_servers.yaml" in blob
    assert "plugin-distribution.md" in blob
    # Handoff is aligned and must not be reported as drift.
    assert "ok: mcp-workstate-handoff pinned at 0.12.0" in blob


def test_single_site_drift_is_isolated(tmp_path: Path) -> None:
    """Only the stale surface is flagged DRIFT; the aligned ones stay ok."""
    module = _load_module()
    repo = _build_repo(
        tmp_path,
        orchestrator_install_pin="0.5.0",  # stale
        orchestrator_yaml_pin="0.5.1",  # current
        orchestrator_doc_pin="0.5.1",  # current
    )
    ok, messages = module.check_all(repo)
    blob = "\n".join(messages)
    assert not ok
    # The install.py line is DRIFT; the yaml line is ok.
    drift_lines = [m for m in messages if "DRIFT" in m]
    assert any("DEFAULT_MCP_SERVERS" in m for m in drift_lines)
    assert not any("mcp_servers.yaml" in m for m in drift_lines)


def test_package_scope_limits_to_one_server(tmp_path: Path) -> None:
    module = _load_module()
    repo = _build_repo(tmp_path)
    ok, messages = module.check_all(repo, package_name="mcp-workstate-orchestrator")
    assert ok, messages
    assert all("handoff" not in m for m in messages)


def test_non_server_package_is_noop(tmp_path: Path) -> None:
    module = _load_module()
    repo = _build_repo(tmp_path)
    ok, messages = module.check_all(repo, package_name="workstate-protocol")
    assert ok
    assert any("not a managed MCP server" in m for m in messages)


def test_cli_exit_code_on_drift(tmp_path: Path) -> None:
    module = _load_module()
    repo = _build_repo(tmp_path, orchestrator_install_pin="0.5.0")
    assert module.main(["--repo-root", str(repo)]) == 1
    aligned = _build_repo(tmp_path / "aligned")
    assert module.main(["--repo-root", str(aligned)]) == 0


def test_handoff_drift_is_detected(tmp_path: Path) -> None:
    """The SECOND managed server has a real negative path: handoff stale on the
    install.py surface (vs its 0.12.0 pyproject) while orchestrator stays
    aligned must flag handoff — and only handoff. Without this, a handoff-only
    name/regex bug would pass every other test silently."""
    module = _load_module()
    repo = _build_repo(tmp_path, handoff_install_pin="0.11.9")  # pyproject is 0.12.0
    ok, messages = module.check_all(repo)
    blob = "\n".join(messages)
    assert not ok
    assert "mcp-workstate-handoff (expected 0.12.0)" in blob
    drift_lines = [m for m in messages if "DRIFT" in m]
    assert any("DEFAULT_MCP_SERVERS" in m for m in drift_lines)
    # Orchestrator stays clean and is not dragged into the failure.
    assert "ok: mcp-workstate-orchestrator pinned at 0.5.1" in blob


def test_stray_version_reference_is_drift(tmp_path: Path) -> None:
    """Conservative whole-file scan (documented fail-closed contract): a stale
    <dist>@<ver> anywhere in a primary site — e.g. a comment — is drift even
    when the load-bearing args[0] pin is correct, so a real second pin can never
    be missed."""
    module = _load_module()
    repo = _build_repo(
        tmp_path,
        extra_install_line="# legacy note: previously mcp-workstate-orchestrator@0.4.0",
    )
    ok, messages = module.check_all(repo)
    blob = "\n".join(messages)
    assert not ok
    drift_lines = [m for m in messages if "DRIFT" in m]
    assert any("DEFAULT_MCP_SERVERS" in m for m in drift_lines)
    # Both versions are surfaced so the operator can see the stray reference.
    assert "0.4.0" in blob and "0.5.1" in blob


def test_pre_release_pin_does_not_truncate_to_base_triple(tmp_path: Path) -> None:
    """Right-anchored version regex: a pre-release / 4-component pin must NOT be
    truncated to its base triple and silently pass. ``@0.5.1rc1`` is a non-match
    (the orchestrator surface then has no recognized pin) => drift, not a false
    OK that ships the superseded build."""
    module = _load_module()
    repo = _build_repo(tmp_path, orchestrator_install_pin="0.5.1rc1")
    ok, messages = module.check_all(repo)
    assert not ok
    drift_lines = [m for m in messages if "DRIFT" in m]
    assert any("DEFAULT_MCP_SERVERS" in m for m in drift_lines)


def test_extras_pin_matches_and_sibling_name_does_not() -> None:
    """PEP 508 extras tolerance: ``<dist>[bridge]@<ver>`` is recognized as a pin
    of ``<dist>`` (the orchestrator's published launch form), while a longer
    sibling name (``<dist>-foo@<ver>``) still never matches ``<dist>``."""
    module = _load_module()
    assert module.versions_in_text(
        "mcp-workstate-orchestrator[bridge]@0.6.0", "mcp-workstate-orchestrator"
    ) == ["0.6.0"]
    assert module.versions_in_text(
        "mcp-workstate-orchestrator@0.6.0", "mcp-workstate-orchestrator"
    ) == ["0.6.0"]
    assert (
        module.versions_in_text(
            "mcp-workstate-orchestrator-foo@0.6.0", "mcp-workstate-orchestrator"
        )
        == []
    )
    # Extras pin with a pre-release version still right-anchors to a non-match.
    assert (
        module.versions_in_text(
            "mcp-workstate-orchestrator[bridge]@0.6.0rc1", "mcp-workstate-orchestrator"
        )
        == []
    )


def test_deleted_primary_pin_is_drift(tmp_path: Path) -> None:
    """Removing the orchestrator pin entirely from a primary site is drift; the
    server stays 'managed' via the other primary site (install.py)."""
    module = _load_module()
    repo = _build_repo(tmp_path, omit_yaml_orchestrator=True)
    ok, messages = module.check_all(repo)
    assert not ok
    drift_lines = [m for m in messages if "DRIFT" in m]
    assert any("mcp_servers.yaml" in m for m in drift_lines)
    assert any("no pin" in m for m in messages)


def test_absent_coupled_doc_is_tolerated(tmp_path: Path) -> None:
    """An absent coupled doc (plugin-distribution.md) is tolerated — only a
    missing PRIMARY pin site is drift. Pins the asymmetry in PinSurface.primary."""
    module = _load_module()
    repo = _build_repo(tmp_path, omit_doc=True)
    ok, messages = module.check_all(repo)
    assert ok, "\n".join(messages)


def test_real_repo_pins_aligned() -> None:
    """Steady-state self-test against the REAL repo: catches a format drift
    between the synthetic fixture and the actual pin sites that would otherwise
    hide behind green synthetic tests. This is the `make check-mcp-pins` smoke
    test embedded in the suite — if it fails, the real pins genuinely disagree."""
    module = _load_module()
    ok, messages = module.check_all(REPO_ROOT)
    assert ok, "\n".join(messages)


# --------------------------------------------------------------------------- #
# Release-bump gate (check_release_bump / scripts/release_prepare.py)
# --------------------------------------------------------------------------- #


def test_release_bump_gate_blocks_stale_server_bump(tmp_path: Path) -> None:
    module = _load_module()
    repo = _build_repo(tmp_path)  # pins all at 0.5.1
    ok, messages = module.check_release_bump(
        repo, "mcp-workstate-orchestrator", "0.5.2"
    )
    blob = "\n".join(messages)
    assert not ok
    assert "Refusing to bump mcp-workstate-orchestrator to 0.5.2" in blob
    assert "expected 0.5.2" in blob


def test_release_bump_gate_passes_when_pins_match(tmp_path: Path) -> None:
    module = _load_module()
    repo = _build_repo(
        tmp_path,
        orchestrator_install_pin="0.5.2",
        orchestrator_yaml_pin="0.5.2",
        orchestrator_doc_pin="0.5.2",
    )
    ok, messages = module.check_release_bump(
        repo, "mcp-workstate-orchestrator", "0.5.2"
    )
    assert ok, messages


def test_release_bump_gate_noop_for_non_server(tmp_path: Path) -> None:
    module = _load_module()
    repo = _build_repo(tmp_path)
    ok, messages = module.check_release_bump(repo, "workstate-protocol", "2.0.0")
    assert ok and messages == []


def test_release_prepare_subprocess_fails_on_stale_pins(tmp_path: Path) -> None:
    """End-to-end: release_prepare.py exits non-zero (no files written) when a
    managed server is bumped while its pins lag."""
    repo = _build_repo(tmp_path, copy_scripts=True)
    pyproject = repo / "packages" / "mcp-workstate-orchestrator" / "pyproject.toml"
    changelog = repo / "packages" / "mcp-workstate-orchestrator" / "CHANGELOG.md"
    pyproject_before = pyproject.read_text(encoding="utf-8")
    changelog_before = changelog.read_text(encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "release_prepare.py"),
            "mcp-workstate-orchestrator",
            "patch",  # 0.5.1 -> 0.5.2, while pins say 0.5.1
            "--allow-dirty",
            "--date",
            "2026-06-02",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "managed MCP-server pins are stale" in result.stderr
    # Gate runs before ANY write — neither pyproject nor changelog is touched.
    assert pyproject.read_text(encoding="utf-8") == pyproject_before
    assert changelog.read_text(encoding="utf-8") == changelog_before


# --------------------------------------------------------------------------- #
# Bridge extra floor coherence (round-3 finding bridge_extra_floor_coherence_
# unguarded): the orchestrator's [bridge] extra floor must bracket the repo's
# workstate-codex-bridge version, or `uvx mcp-workstate-orchestrator[bridge]@v`
# silently resolves a stale bridge wheel after a bridge major bump.
# --------------------------------------------------------------------------- #


def _add_bridge_floor(
    repo: Path,
    *,
    bridge_version: str = "0.1.1",
    floor: str = ">=0.1.0,<0.2.0",
) -> None:
    manifest_path = repo / "config" / "release" / "packages.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["packages"].append(
        {
            "name": "workstate-codex-bridge",
            "path": "packages/workstate-codex-bridge",
            "distribution": "workstate-codex-bridge",
            "changelog": "packages/workstate-codex-bridge/CHANGELOG.md",
        }
    )
    _write(manifest_path, json.dumps(manifest, indent=2) + "\n")
    _write(
        repo / "packages" / "workstate-codex-bridge" / "pyproject.toml",
        f'[project]\nname = "workstate-codex-bridge"\nversion = "{bridge_version}"\n',
    )
    orchestrator_pyproject = (
        repo / "packages" / "mcp-workstate-orchestrator" / "pyproject.toml"
    )
    orchestrator_pyproject.write_text(
        orchestrator_pyproject.read_text(encoding="utf-8")
        + "\n[project.optional-dependencies]\n"
        + f'bridge = ["workstate-codex-bridge{floor}"]\n',
        encoding="utf-8",
    )


def test_bridge_floor_admitting_published_version_passes(tmp_path: Path) -> None:
    module = _load_module()
    repo = _build_repo(tmp_path)
    _add_bridge_floor(repo, bridge_version="0.1.1", floor=">=0.1.0,<0.2.0")
    ok, messages = module.check_all(repo)
    assert ok, "\n".join(messages)
    assert any("bridge" in m and "0.1.1" in m for m in messages)


def test_bridge_floor_excluding_published_version_is_drift(tmp_path: Path) -> None:
    # The silent-skew moment: bridge bumped to 0.2.0 while the orchestrator
    # extra still says <0.2.0 — uvx would resolve the stale 0.1.x wheel.
    module = _load_module()
    repo = _build_repo(tmp_path)
    _add_bridge_floor(repo, bridge_version="0.2.0", floor=">=0.1.0,<0.2.0")
    ok, messages = module.check_all(repo)
    blob = "\n".join(messages)
    assert not ok
    assert "workstate-codex-bridge" in blob
    assert "0.2.0" in blob
    assert ">=0.1.0,<0.2.0" in blob


def test_repo_without_bridge_extra_skips_floor_check(tmp_path: Path) -> None:
    module = _load_module()
    repo = _build_repo(tmp_path)
    ok, messages = module.check_bridge_extra_floor(repo)
    assert ok, "\n".join(messages)


def test_release_bump_gate_blocks_bridge_bump_outside_floor(tmp_path: Path) -> None:
    module = _load_module()
    repo = _build_repo(tmp_path)
    _add_bridge_floor(repo, bridge_version="0.1.1", floor=">=0.1.0,<0.2.0")
    ok, messages = module.check_release_bump(repo, "workstate-codex-bridge", "0.2.0")
    blob = "\n".join(messages)
    assert not ok
    assert ">=0.1.0,<0.2.0" in blob


def test_release_bump_gate_passes_bridge_bump_inside_floor(tmp_path: Path) -> None:
    module = _load_module()
    repo = _build_repo(tmp_path)
    _add_bridge_floor(repo, bridge_version="0.1.1", floor=">=0.1.0,<0.2.0")
    ok, messages = module.check_release_bump(repo, "workstate-codex-bridge", "0.1.2")
    assert ok, "\n".join(messages)


def test_real_repo_bridge_floor_coherent() -> None:
    module = _load_module()
    ok, messages = module.check_bridge_extra_floor(REPO_ROOT)
    assert ok, "\n".join(messages)
