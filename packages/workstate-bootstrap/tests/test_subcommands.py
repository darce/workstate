"""Subcommand contracts for ``workstate-bootstrap status / doctor / update / repair``.

These tests pin the smallest end-to-end behavior of each post-install
subcommand. They use the ``fake_remote`` and ``fake_remote_with_surfaces``
fixtures from ``test_install.py`` via direct import so the fixtures stay in
one place.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest
import yaml

from tests.test_install import (  # noqa: F401
    _workstate_system_root,
    _handoff_src_dir,
    SHARED_GIT_HOOK_NAMES,
    SHARED_HOOK_HELPER_NAMES,
    SHARED_SURFACES_EXPECTED,
    fake_remote,
    fake_remote_with_generator,
    fake_remote_with_surfaces,
)


SAMPLE_MCP_SERVERS = {
    "workstate-handoff-mcp": {
        "command": sys.executable,
        "args": ["-m", "workstate_handoff_mcp"],
        "env": {"PYTHONPATH": str(_handoff_src_dir())},
    },
}


@pytest.fixture(autouse=True)
def _isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolate ``HOME`` for every test in this module (WORKSTATE-REF-09).

    The doctor grok facets (``_doctor_grok_plugin_materialization``) read the
    user-global ``~/.grok/config.toml``; without isolation the operator's real
    grok state (stale selectors, disabled bare selector) leaks into
    ``doctor() == []`` assertions and makes the suite environment-dependent.
    Tests that need a specific grok config set HOME themselves, which
    overrides this fixture.
    """
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("isolated-home")))


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


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_prints_manifest_summary(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """``status(target=...)`` reads the manifest and returns a multi-line
    human-readable summary that names the remote, the resolved SHA, and the
    counts of materialized surfaces and touched configs."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import status

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    summary = status(target=target)

    assert manifest["remote_sha"] in summary
    assert url in summary
    assert ref in summary
    # ``fake_remote_with_surfaces`` materializes 6 shared surfaces and the
    # install prepares 1 generated per-agent surface — 7 total. Assert the
    # count the summary reports round-trips from the manifest rather than
    # substring-matching a bare digit (which would also match the
    # non-deterministic remote SHA or a tmp-path component).
    assert f"surfaces:    {len(manifest['surfaces'])} (" in summary
    assert "shared" in summary
    assert "generated" in summary
    # The managed install writes five configs: the three MCP files, the
    # consumer Makefile (lifecycle include), and the git ``core.hooksPath``
    # entry. Pin the reported count (round-tripped from the manifest) and the
    # presence of each config path. The previous ``"4" in summary`` check was
    # non-hermetic: the real count is 5, so the assertion only ever passed
    # because the digit happened to appear in the commit SHA or tmp path.
    config_paths = {entry["path"] for entry in manifest["configs"]}
    assert {
        ".mcp.json",
        ".vscode/mcp.json",
        ".codex/config.toml",
        "Makefile",
        "core.hooksPath",
    } <= config_paths
    assert f"configs:     {len(manifest['configs'])}" in summary
    for path in config_paths:
        assert f"- {path} (" in summary


def test_status_reports_handoff_state_paths_and_schema_version(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """PLAN0003-S4-ST-001: after a managed install, status must invoke
    init-state --check and append the resolved state_dir, db_path,
    exports_dir, and schema_version to the summary so operators can see
    the cold-start contract was satisfied."""
    from workstate_handoff_mcp.shared_schema import HANDOFF_SCHEMA_VERSION

    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import status

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    summary = status(target=target)

    assert "handoff state:" in summary
    assert str(target / ".task-state") in summary
    assert str(target / ".task-state" / "handoff.db") in summary
    assert str(target / ".task-state" / "exports") in summary
    assert f"schema_version: {HANDOFF_SCHEMA_VERSION}" in summary


def test_status_omits_handoff_state_section_for_no_mcp_servers_install(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """PLAN0003-S4-ST-002: a --no-mcp-servers install does not register
    init-state, so status must not attempt to query it (no
    'handoff state:' section in the summary)."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import status

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=None,
    )

    summary = status(target=target)
    assert "handoff state:" not in summary


def test_status_reports_malformed_mcp_servers_without_crashing(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """A ``.mcp.json`` whose ``mcpServers`` key is a scalar (valid JSON, wrong
    shape) must surface a clean error line rather than raising AttributeError
    mid-render, mirroring the function's existing JSON-decode error handling."""
    import json

    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import status

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    (target / ".mcp.json").write_text(
        json.dumps({"mcpServers": "not-a-table"}), encoding="utf-8"
    )

    summary = status(target=target)
    assert "mcpServers is malformed" in summary


def test_status_missing_manifest_raises(tmp_path: Path) -> None:
    """``status`` against a target that was never installed must raise
    ``FileNotFoundError`` rather than silently returning a stub."""
    from workstate_bootstrap.subcommands import status

    target = tmp_path / "uninstalled"
    target.mkdir()

    with pytest.raises(FileNotFoundError):
        status(target=target)


def test_status_console_script(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """``python -m workstate_bootstrap status --target <path>`` exits 0 and
    prints the same summary."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "workstate_bootstrap",
            "status",
            "--target",
            str(target),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert manifest["remote_sha"] in result.stdout


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_clean_install_returns_no_findings(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """A fresh install must produce zero doctor findings."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    findings = doctor(target=target)
    assert findings == []


def test_doctor_detects_missing_clone(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """When `.workstate/remote/.git` is gone, doctor must flag it."""
    import shutil

    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )
    shutil.rmtree(target / ".workstate" / "remote")

    findings = doctor(target=target)
    kinds = {f["kind"] for f in findings}
    assert "missing_clone" in kinds


def test_doctor_detects_broken_surface_symlink(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """When a shared surface is recorded as `shared` in the manifest but the
    symlink no longer resolves into the clone, doctor must flag it."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    # Break the scripts/hooks symlink: replace it with a stub directory.
    hooks_link = target / "scripts" / "hooks"
    hooks_link.unlink()
    hooks_link.mkdir()

    findings = doctor(target=target)
    kinds_paths = {(f["kind"], f["path"]) for f in findings}
    assert ("surface_drift", "scripts/hooks") in kinds_paths


def _import_overlay_resolver():
    """Import the workstate-system overlay resolver from its sibling package.

    The bootstrap package has no dependency on workstate-system, so the test
    reaches the resolver the same way the workstate-system suite does: by
    inserting the package root on ``sys.path`` and importing ``scripts.*``.
    """
    system_root = Path(__file__).resolve().parents[2] / "workstate-system"
    if str(system_root) not in sys.path:
        sys.path.insert(0, str(system_root))
    from scripts.overlay_resolver import BrokenOverlayError, resolve_surface

    return BrokenOverlayError, resolve_surface


def test_overlay_resolver_and_doctor_agree_on_broken_shared_surface(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """WS-HARNESS-FAILSAFE-01 implementation note: bootstrap doctor and the workstate-system
    overlay resolver must report compatible outcomes for the same broken
    canonical shared surface — clean before, drift/fail-closed after."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    BrokenOverlayError, resolve_surface = _import_overlay_resolver()

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces
    install(
        target=target, remote_url=url, remote_ref=ref, mcp_servers=SAMPLE_MCP_SERVERS
    )

    # Clean canonical install: doctor finds no drift and the resolver resolves
    # the shared `hooks` surface (scripts/hooks is one of its ledger paths).
    assert doctor(target=target) == []
    assert resolve_surface("hooks", target)  # non-empty, no exception

    # Break the bootstrap-managed scripts/hooks shared symlink.
    hooks_link = target / "scripts" / "hooks"
    hooks_link.unlink()
    hooks_link.mkdir()

    # doctor side: surface_drift for the broken shared surface.
    kinds_paths = {(f["kind"], f["path"]) for f in doctor(target=target)}
    assert ("surface_drift", "scripts/hooks") in kinds_paths

    # resolver side: the same broken shared surface fails closed instead of
    # silently falling back to source-tree roots.
    with pytest.raises(BrokenOverlayError):
        resolve_surface("hooks", target)


def test_overlay_manifest_contract_documents_canonical_and_legacy() -> None:
    """WS-HARNESS-FAILSAFE-01 implementation note: the overlay-manifest contract doc must
    name the canonical `.workstate-bootstrap.json` ledger as well as the legacy
    `.workstate-overlay.json` read-compat manifest."""
    # implementation note S3: docs/workstate/contracts moved into the package payload tree.
    contract = (
        Path(__file__).resolve().parents[2]
        / "workstate-system"
        / "workstate_system"
        / "payload"
        / "docs"
        / "workstate"
        / "contracts"
        / "overlay-manifest.yaml"
    )
    text = contract.read_text(encoding="utf-8")
    assert ".workstate-bootstrap.json" in text, (
        "must document the canonical bootstrap ledger"
    )
    assert ".workstate-overlay.json" in text, (
        "must still document the legacy overlay manifest"
    )
    lowered = text.lower()
    assert "canonical" in lowered and "legacy" in lowered


def test_doctor_detects_drift_in_generated_surface(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """When a per-agent generated artifact has been hand-edited away from
    what the canonical sources would produce, doctor must emit a
    ``generated_drift`` finding pointing at the owning surface."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    # Tamper with a generated Copilot prompt. WORKSTATE-REF-02 implementation note cutover:
    # ``.github/prompts`` is the remaining per-agent generated surface.
    prompt_files = list((target / ".github" / "prompts").glob("*.prompt.md"))
    assert prompt_files, "fixture must have produced at least one Copilot prompt"
    prompt_files[0].write_text(prompt_files[0].read_text() + "\nhand-edited drift\n")

    findings = doctor(target=target)
    kinds_paths = {(f["kind"], f["path"]) for f in findings}
    assert ("generated_drift", ".github/prompts") in kinds_paths


def _seed_branch_review_override(target: Path, system_root: Path) -> None:
    override_root = target / "workstate-overrides" / "workstate-system"
    _seed_branch_review_override_at(override_root, system_root)


def _seed_branch_review_override_at(override_root: Path, system_root: Path) -> None:
    override_skill_dir = override_root / "skills" / "branch-review"
    override_skill_dir.mkdir(parents=True)
    (override_skill_dir / "SKILL.md").write_text(
        "---\nname: branch-review\ndescription: local override\n---\n\nBootstrap-composed override body.\n"
    )

    # implementation note S3: skills/ moved into the package payload tree. Read the real
    # upstream skill source from payload; the override layout above stays.
    payload_root = system_root / "workstate_system" / "payload"
    structured = yaml.safe_load(
        (payload_root / "skills" / "branch-review" / "skill.yaml").read_text()
    )
    structured.pop("generator", None)
    body = (payload_root / "skills" / "branch-review" / "body.md").read_text()
    fm_text = yaml.safe_dump(
        structured, sort_keys=False, default_flow_style=False
    ).rstrip()
    # Mirror _render_plugin_skill: manifest global_instructions are prepended
    # to every emitted SKILL.md body, so the rendered upstream digest must
    # include the section.
    manifest_payload = json.loads(
        (
            payload_root / "config" / "agent-workflows" / "portable_commands.json"
        ).read_text()
    )
    instructions = manifest_payload.get("global_instructions") or []
    if instructions:
        body = (
            "## Global Instructions\n\n"
            + "\n".join(f"- {item}" for item in instructions)
            + "\n\n"
            + body
        )
    base_skill = (
        f"---\n{fm_text}\n---\n\n{body if body.endswith(chr(10)) else body + chr(10)}"
    )
    upstream_digest = hashlib.sha256(base_skill.encode("utf-8")).hexdigest()
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "skills": {
                        "branch-review": {
                            "mode": "replace",
                            "path": "skills/branch-review/SKILL.md",
                            "upstream_digest": f"sha256:{upstream_digest}",
                            "on_upstream_change": "warn",
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )


def test_repair_from_adopted_worktree_preserves_primary_overrides(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """WORKSTATE-REF-07 dogfood regression: an adopted linked worktree shares the
    primary's ``.workstate/generated`` via symlink, but the override root is
    untracked and therefore absent in the worktree checkout. ``repair`` run
    from the worktree must still compose the shared effective tree WITH the
    primary's overrides instead of silently stripping them to passthrough."""
    from workstate_bootstrap.adopt import adopt_worktree
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import repair

    system_root = _workstate_system_root()
    if system_root is None:
        pytest.skip("packages/workstate-system not available in this environment")

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    _seed_branch_review_override(target, system_root)

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    effective_skill = target.joinpath(
        ".workstate",
        "generated",
        "plugins",
        "workstate-system",
        "effective",
        "claude",
        "skills",
        "branch-review",
        "SKILL.md",
    )
    assert "Bootstrap-composed override body." in effective_skill.read_text()

    # Real dogfood shape: the consumer tracks the bootstrap ledger but NOT
    # the override root, so a linked worktree checkout carries the ledger
    # while workstate-overrides/ is absent there.
    _git("add", "-f", ".workstate-bootstrap.json", cwd=target)
    _git("commit", "-m", "track bootstrap ledger", cwd=target)

    wt = tmp_path / "wt"
    _git("worktree", "add", str(wt), cwd=target)
    assert not (wt / "workstate-overrides").exists()
    adopt_worktree(target=wt, primary=target)

    repair(target=wt)

    assert "Bootstrap-composed override body." in effective_skill.read_text(), (
        "repair from an adopted worktree recomposed the shared effective tree "
        "without the primary's override root"
    )


def test_doctor_detects_missing_effective_plugin_tree_when_overrides_exist(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """Override-aware installs pin Claude/Codex at the generated effective
    tree, so doctor must flag generated drift when that tree disappears."""
    import shutil

    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    system_root = _workstate_system_root()
    if system_root is None:
        pytest.skip("packages/workstate-system not available in this environment")

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    _seed_branch_review_override(target, system_root)

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    shutil.rmtree(
        target
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "effective"
    )

    findings = doctor(target=target)
    kinds_paths = {(f["kind"], f["path"]) for f in findings}
    assert (
        "generated_drift",
        ".workstate/generated/plugins/workstate-system/effective",
    ) in kinds_paths


def test_repair_restores_missing_effective_plugin_tree_when_overrides_exist(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """Repair must regenerate the effective plugin tree when an override-aware
    install loses it after bootstrap."""
    import shutil

    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor, repair

    system_root = _workstate_system_root()
    if system_root is None:
        pytest.skip("packages/workstate-system not available in this environment")

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    _seed_branch_review_override(target, system_root)

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    effective_root = (
        target
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "effective"
    )
    shutil.rmtree(effective_root)
    assert any(
        f["path"] == ".workstate/generated/plugins/workstate-system/effective"
        for f in doctor(target=target)
    )

    # Guard against real `grok plugin install/enable` side-effects: repair's
    # grok branch shells out when the operator's machine has the grok CLI.
    from unittest.mock import patch

    with patch(
        "workstate_bootstrap.activation._grok_cli_available", return_value=False
    ):
        report = repair(target=target)
    assert any(
        entry["path"] == ".workstate/generated/plugins/workstate-system/effective"
        for entry in report["repaired"]
    ), report
    assert (effective_root / "plugin-lock.json").is_file()
    assert doctor(target=target) == []


def test_doctor_and_repair_handle_zero_override_effective_tree(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """WORKSTATE-REF-07 always-effective installs have no override root, but the
    effective tree is still managed and checked with passthrough lock mode."""
    import shutil

    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor, repair

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)
    assert not any(
        finding["kind"] == "generated_drift"
        and finding["path"] == ".workstate/generated/plugins/workstate-system/effective"
        for finding in doctor(target=target)
    )

    effective_root = (
        target
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "effective"
    )
    shutil.rmtree(effective_root)

    findings = doctor(target=target)
    assert any(
        finding["kind"] == "generated_drift"
        and finding["path"] == ".workstate/generated/plugins/workstate-system/effective"
        for finding in findings
    )

    report = repair(target=target)
    assert any(
        entry["path"] == ".workstate/generated/plugins/workstate-system/effective"
        for entry in report["repaired"]
    ), report
    assert (effective_root / "plugin-lock.json").is_file()
    assert not any(
        finding["kind"] == "generated_drift"
        and finding["path"] == ".workstate/generated/plugins/workstate-system/effective"
        for finding in doctor(target=target)
    )


def test_repair_re_runs_generator_for_generated_drift(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """Repair must re-run the generator and clear ``generated_drift``."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor, repair

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    prompt_files = list((target / ".github" / "prompts").glob("*.prompt.md"))
    assert prompt_files, "fixture must have produced at least one Copilot prompt"
    drifted = prompt_files[0]
    drifted.write_text(drifted.read_text() + "\nhand-edited drift\n")
    assert any(f["kind"] == "generated_drift" for f in doctor(target=target))

    report = repair(target=target)
    assert any(e["kind"] == "generated_drift" for e in report["repaired"]), report

    # After repair, doctor reports no generated drift.
    follow_up = doctor(target=target)
    assert not any(f["kind"] == "generated_drift" for f in follow_up), follow_up


def test_doctor_detects_drifted_managed_mcp_server(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """When `.mcp.json` no longer carries the managed server config, doctor
    must flag a `config_drift` for `.mcp.json`."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    mcp_path = target / ".mcp.json"
    doc = json.loads(mcp_path.read_text())
    del doc["mcpServers"]["workstate-handoff-mcp"]
    mcp_path.write_text(json.dumps(doc, indent=2))

    findings = doctor(target=target, mcp_servers=SAMPLE_MCP_SERVERS)
    kinds_paths = {(f["kind"], f["path"]) for f in findings}
    assert ("config_drift", ".mcp.json") in kinds_paths


def test_doctor_flags_missing_handoff_db_when_mcp_servers_registered(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """PLAN0003-S4-DR-001: install registered .mcp.json -> doctor must flag
    missing .task-state/handoff.db as state_drift."""
    import shutil

    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    # Sanity: install wrote .mcp.json into configs and produced handoff.db.
    manifest = json.loads((target / ".workstate-bootstrap.json").read_text())
    paths = {entry["path"] for entry in manifest["configs"]}
    assert ".mcp.json" in paths
    assert (target / ".task-state" / "handoff.db").is_file()

    shutil.rmtree(target / ".task-state")

    findings = doctor(target=target)
    kinds_paths = {(f["kind"], f["path"]) for f in findings}
    assert ("state_drift", ".task-state/handoff.db") in kinds_paths


def test_doctor_suppresses_state_check_after_no_mcp_servers_install(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """PLAN0003-S4-DR-002: install with mcp_servers=None must NOT register
    .mcp.json in the manifest, and doctor must NOT raise state_drift even
    though .task-state/handoff.db is intentionally absent."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=None,
    )

    manifest = json.loads((target / ".workstate-bootstrap.json").read_text())
    paths = {entry["path"] for entry in manifest["configs"]}
    assert ".mcp.json" not in paths
    assert not (target / ".task-state").exists()

    findings = doctor(target=target)
    kinds = {f["kind"] for f in findings}
    assert "state_drift" not in kinds


def test_doctor_console_script_exit_codes(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """`workstate-bootstrap doctor --target` exits 0 when clean, 1 when drift
    is detected."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    clean = subprocess.run(
        [
            sys.executable,
            "-m",
            "workstate_bootstrap",
            "doctor",
            "--target",
            str(target),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert clean.returncode == 0, clean.stderr

    (target / "scripts" / "hooks").unlink()

    dirty = subprocess.run(
        [
            sys.executable,
            "-m",
            "workstate_bootstrap",
            "doctor",
            "--target",
            str(target),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert dirty.returncode == 1
    assert "surface_drift" in dirty.stdout or "surface_drift" in dirty.stderr


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_remote_with_surfaces_two_refs(tmp_path: Path) -> tuple[str, str, str]:
    """Build a local bare git remote with required shared surfaces on two refs."""
    src = tmp_path / "remote-src"
    src.mkdir()
    _git("init", "--initial-branch=main", cwd=src)
    _git("config", "user.email", "test@example.com", cwd=src)
    _git("config", "user.name", "Test", cwd=src)

    for surface in (".github/hooks", "scripts/hooks", "docs/workstate/contracts"):
        surface_dir = src / surface
        surface_dir.mkdir(parents=True)
        (surface_dir / "MARKER.md").write_text(f"shared {surface} v1\n")

    _git("add", "-A", cwd=src)
    _git("commit", "-m", "v1", cwd=src)
    _git("tag", "v0.1.0", cwd=src)

    (src / "docs" / "workstate" / "contracts" / "MARKER.md").write_text(
        "shared docs/workstate/contracts v2\n"
    )
    _git("add", "-A", cwd=src)
    _git("commit", "-m", "v2", cwd=src)
    _git("tag", "v0.2.0", cwd=src)

    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(src), str(bare), cwd=tmp_path)
    return f"file://{bare}", "v0.1.0", "v0.2.0"


@pytest.fixture()
def fake_remote_with_two_refs(tmp_path: Path) -> tuple[str, str, str]:
    """Build a local bare git remote shipping two refs (v0.1.0 and v0.2.0)
    with different content so the resolved SHA changes between updates."""
    src = tmp_path / "remote-src"
    src.mkdir()
    _git("init", "--initial-branch=main", cwd=src)
    _git("config", "user.email", "test@example.com", cwd=src)
    _git("config", "user.name", "Test", cwd=src)
    (src / "skill.md").write_text("# v1\n")
    _git("add", "-A", cwd=src)
    _git("commit", "-m", "v1", cwd=src)
    _git("tag", "v0.1.0", cwd=src)
    (src / "skill.md").write_text("# v2\n")
    _git("add", "-A", cwd=src)
    _git("commit", "-m", "v2", cwd=src)
    _git("tag", "v0.2.0", cwd=src)

    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(src), str(bare), cwd=tmp_path)
    return f"file://{bare}", "v0.1.0", "v0.2.0"


def _build_generator_two_ref_remote(
    tmp_path: Path, mutate_v2: Callable[[Path], None]
) -> tuple[str, str, str]:
    """Build a generator-backed two-ref remote, applying ``mutate_v2`` before
    the second tagged commit."""
    import shutil

    system_root = _workstate_system_root()
    if system_root is None:
        pytest.skip("packages/workstate-system not available in this environment")

    # implementation note S3: shipped overlay surfaces now live under the package's
    # ``workstate_system/payload/`` tree. Reads of the REAL package source
    # resolve payload-first, then fall back to the legacy top-level path for
    # surfaces that stayed (e.g. ``Makefile.d/evals.mk``, ``scripts/workstate/
    # evals``). The fake remote still mirrors the OLD consumer layout.
    payload_root = system_root / "workstate_system" / "payload"

    def _real_surface_dirs(rel: str) -> list[Path]:
        return [d for d in (payload_root / rel, system_root / rel) if d.is_dir()]

    def _merge_copytree(sources: list[Path], target_dir: Path) -> None:
        for source_dir in sources:
            shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)

    src = tmp_path / "gen-two-refs-src"
    src.mkdir()
    _git("init", "--initial-branch=main", cwd=src)
    _git("config", "user.email", "test@example.com", cwd=src)
    _git("config", "user.name", "Test", cwd=src)

    system_subdir = src / "packages" / "workstate-system"
    for surface in SHARED_SURFACES_EXPECTED:
        source_dirs = _real_surface_dirs(surface)
        target_dir = system_subdir / surface
        if source_dirs and any(any(d.iterdir()) for d in source_dirs):
            _merge_copytree(source_dirs, target_dir)
        else:
            target_dir.mkdir(parents=True)
            (target_dir / "MARKER.md").write_text(f"shared {surface}\n")

    git_hooks_dir = system_subdir / "scripts" / "hooks" / "git"
    git_hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in SHARED_GIT_HOOK_NAMES:
        hook = git_hooks_dir / name
        if hook.exists():
            continue
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
    for helper in SHARED_HOOK_HELPER_NAMES:
        helper_path = system_subdir / "scripts" / "hooks" / helper
        if helper_path.exists():
            continue
        helper_path.write_text("#!/usr/bin/env python3\nimport sys; sys.exit(0)\n")
        helper_path.chmod(0o755)

    (system_subdir / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        payload_root / "scripts" / "generate_agent_workflows.py",
        system_subdir / "scripts" / "generate_agent_workflows.py",
    )
    # implementation note implementation note: the generator loads its fail-open wrap transform from
    # the sibling _guard_wrap.py; the two ship together in every layout.
    shutil.copy2(
        payload_root / "scripts" / "_guard_wrap.py",
        system_subdir / "scripts" / "_guard_wrap.py",
    )
    shutil.copytree(
        payload_root / "config" / "agent-workflows",
        system_subdir / "config" / "agent-workflows",
    )
    shutil.copytree(payload_root / "skills", system_subdir / "skills")

    _git("add", "-A", cwd=src)
    _git("commit", "-m", "v1", cwd=src)
    _git("tag", "v0.1.0", cwd=src)

    mutate_v2(system_subdir)
    _git("add", "-A", cwd=src)
    _git("commit", "-m", "v2", cwd=src)
    _git("tag", "v0.2.0", cwd=src)

    bare = tmp_path / "gen-two-refs.git"
    _git("clone", "--bare", str(src), str(bare), cwd=tmp_path)
    return f"file://{bare}", "v0.1.0", "v0.2.0"


@pytest.fixture()
def fake_remote_with_generator_two_refs(tmp_path: Path) -> tuple[str, str, str]:
    """Generator-backed remote with two refs so update() can refresh the
    overlay while leaving consumer override roots intact."""

    def mutate_v2(system_subdir: Path) -> None:
        (system_subdir / "docs" / "workstate" / "contracts" / "UPDATE.md").write_text(
            "updated shared contract marker\n"
        )

    return _build_generator_two_ref_remote(tmp_path, mutate_v2)


@pytest.fixture()
def fake_remote_with_generator_two_refs_stale_branch_review(
    tmp_path: Path,
) -> tuple[str, str, str]:
    """Generator-backed remote whose second ref changes the upstream
    branch-review skill body, making a local replacement override stale."""

    def mutate_v2(system_subdir: Path) -> None:
        branch_review_body = system_subdir / "skills" / "branch-review" / "body.md"
        branch_review_body.write_text(
            branch_review_body.read_text() + "\nUpstream drift marker.\n"
        )

    return _build_generator_two_ref_remote(tmp_path, mutate_v2)


def test_update_advances_remote_sha(
    tmp_path: Path, fake_remote_with_two_refs: tuple[str, str, str]
) -> None:
    """`update(target, remote_ref=<new>)` re-runs install and the manifest's
    remote_sha must change to the new ref's tip."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import update

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref_old, ref_new = fake_remote_with_two_refs

    install(target=target, remote_url=url, remote_ref=ref_old)
    sha_before = json.loads((target / ".workstate-bootstrap.json").read_text())[
        "remote_sha"
    ]

    manifest = update(
        target=target, remote_ref=ref_new, enforce_required_surfaces=False
    )

    assert manifest["remote_ref"] == ref_new
    assert manifest["remote_sha"] != sha_before


def test_update_uses_existing_manifest_remote_url(
    tmp_path: Path, fake_remote_with_two_refs: tuple[str, str, str]
) -> None:
    """`update` reads remote_url from the existing manifest by default so the
    caller doesn't need to repeat it."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import update

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref_old, ref_new = fake_remote_with_two_refs

    install(target=target, remote_url=url, remote_ref=ref_old)

    manifest = update(
        target=target, remote_ref=ref_new, enforce_required_surfaces=False
    )
    assert manifest["remote_url"] == url


def test_update_preserves_managed_mcp_registration_when_flag_omitted(
    tmp_path: Path, fake_remote_with_surfaces_two_refs: tuple[str, str, str]
) -> None:
    """PLAN0003-S4-UP-001: managed installs must retain MCP registration
    across update() calls even when the caller omits mcp_servers."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import status, update

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref_old, ref_new = fake_remote_with_surfaces_two_refs

    install(
        target=target,
        remote_url=url,
        remote_ref=ref_old,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    manifest = update(target=target, remote_ref=ref_new)
    config_paths = {entry["path"] for entry in manifest["configs"]}

    assert ".mcp.json" in config_paths
    assert ".vscode/mcp.json" in config_paths
    assert ".codex/config.toml" in config_paths
    assert "handoff state:" in status(target=target)


def test_update_preserves_override_root_and_recomposes_effective_tree(
    tmp_path: Path, fake_remote_with_generator_two_refs: tuple[str, str, str]
) -> None:
    """Override-aware installs must preserve local override files across
    update() and keep the effective plugin tree pinned to the override body."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import update

    system_root = _workstate_system_root()
    if system_root is None:
        pytest.skip("packages/workstate-system not available in this environment")

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    _seed_branch_review_override(target, system_root)
    url, ref_old, ref_new = fake_remote_with_generator_two_refs

    install(
        target=target,
        remote_url=url,
        remote_ref=ref_old,
        enforce_required_surfaces=False,
    )
    override_skill = (
        target
        / "workstate-overrides"
        / "workstate-system"
        / "skills"
        / "branch-review"
        / "SKILL.md"
    )
    effective_skill = (
        target
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "effective"
        / "claude"
        / "skills"
        / "branch-review"
        / "SKILL.md"
    )
    lock_path = (
        target
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "effective"
        / "plugin-lock.json"
    )
    sha_before = json.loads((target / ".workstate-bootstrap.json").read_text())[
        "remote_sha"
    ]

    manifest = update(
        target=target, remote_ref=ref_new, enforce_required_surfaces=False
    )

    assert manifest["remote_sha"] != sha_before
    assert "Bootstrap-composed override body." in override_skill.read_text()
    assert "Bootstrap-composed override body." in effective_skill.read_text()
    assert (
        json.loads(lock_path.read_text())["base_remote_sha"] == manifest["remote_sha"]
    )


def test_update_uses_recorded_plugin_override_root(
    tmp_path: Path, fake_remote_with_generator_two_refs: tuple[str, str, str]
) -> None:
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import update

    system_root = _workstate_system_root()
    if system_root is None:
        pytest.skip("packages/workstate-system not available in this environment")

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    override_root = target / "custom-overrides" / "workstate-system"
    _seed_branch_review_override_at(override_root, system_root)
    url, ref_old, ref_new = fake_remote_with_generator_two_refs

    install(
        target=target,
        remote_url=url,
        remote_ref=ref_old,
        plugin_overrides=override_root,
        enforce_required_surfaces=False,
    )

    manifest = update(
        target=target, remote_ref=ref_new, enforce_required_surfaces=False
    )

    effective_skill = (
        target
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "effective"
        / "claude"
        / "skills"
        / "branch-review"
        / "SKILL.md"
    )
    assert manifest["plugin_overrides_path"] == "custom-overrides/workstate-system"
    assert "Bootstrap-composed override body." in effective_skill.read_text()


def test_update_reports_stale_override_after_upstream_skill_change(
    tmp_path: Path,
    fake_remote_with_generator_two_refs_stale_branch_review: tuple[str, str, str],
) -> None:
    """When an upstream skill changes under a warn-mode replacement override,
    update() must preserve the local override and doctor() must report it as stale."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor, update

    system_root = _workstate_system_root()
    if system_root is None:
        pytest.skip("packages/workstate-system not available in this environment")

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    _seed_branch_review_override(target, system_root)
    url, ref_old, ref_new = fake_remote_with_generator_two_refs_stale_branch_review

    install(
        target=target,
        remote_url=url,
        remote_ref=ref_old,
        enforce_required_surfaces=False,
    )
    update(target=target, remote_ref=ref_new, enforce_required_surfaces=False)

    effective_skill = (
        target
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "effective"
        / "claude"
        / "skills"
        / "branch-review"
        / "SKILL.md"
    )
    assert "Bootstrap-composed override body." in effective_skill.read_text()

    kinds_paths = {
        (finding["kind"], finding["path"]) for finding in doctor(target=target)
    }
    assert (
        "stale_override",
        "workstate-overrides/workstate-system/skills/branch-review/SKILL.md",
    ) in kinds_paths


def _plugin_own_mcp_registration(url: str, tmp_path: Path) -> str:
    """Flip ``registration.claude`` to ``plugin`` in the fake remote's
    mcp_servers.yaml and return the new ref.

    implementation note: under the canonical all-root ownership table the generator
    refuses ``components.mcp_servers`` overrides outright (no emitted
    .mcp.json would consume them), so doctor tests that need a tracked MCP
    patch override to survive install must run against a plugin-owned
    registration variant."""
    work = tmp_path / "remote-plugin-owned"
    _git("clone", url, str(work), cwd=tmp_path)
    manifest = (
        work
        / "packages"
        / "workstate-system"
        / "config"
        / "agent-workflows"
        / "mcp_servers.yaml"
    )
    payload = yaml.safe_load(manifest.read_text())
    payload["registration"]["claude"] = "plugin"
    manifest.write_text(yaml.safe_dump(payload, sort_keys=False))
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "flip claude MCP registration to plugin ownership", cwd=work)
    _git("tag", "v0.1.1", cwd=work)
    _git("push", "origin", "main", "v0.1.1", cwd=work)
    return "v0.1.1"


def test_doctor_reports_unsafe_mcp_patch_override_from_tracked_lock(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    override_root = target / "workstate-overrides" / "workstate-system"
    (override_root / "tools").mkdir(parents=True)
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "mcp_servers": {
                        "workstate-handoff-mcp": {
                            "mode": "patch",
                            "patch_path": "tools/mcp_servers.patch.yaml",
                            "requires_trust_ack": True,
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )
    (override_root / "tools" / "mcp_servers.patch.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "target_server": "workstate-handoff-mcp",
                "ops": [
                    {
                        "op": "replace_args",
                        "value": [
                            "mcp-workstate-handoff@0.11.4",
                            "--profile",
                            "consumer",
                        ],
                    },
                    {
                        "op": "upsert_env",
                        "name": "HANDOFF_PROFILE",
                        "value": "consumer",
                    },
                ],
            },
            sort_keys=False,
        )
    )

    url, _ = fake_remote_with_generator
    ref = _plugin_own_mcp_registration(url, tmp_path)
    install(
        target=target, remote_url=url, remote_ref=ref, enforce_required_surfaces=False
    )

    kinds_paths = {
        (finding["kind"], finding["path"]) for finding in doctor(target=target)
    }
    assert (
        "unsafe_tool_patch",
        "workstate-overrides/workstate-system/tools/mcp_servers.patch.yaml",
    ) in kinds_paths


def test_doctor_reports_invalid_override_schema_for_malformed_mcp_patch(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    override_root = target / "workstate-overrides" / "workstate-system"
    patch_file = override_root / "tools" / "mcp_servers.patch.yaml"
    (override_root / "tools").mkdir(parents=True)
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "mcp_servers": {
                        "workstate-handoff-mcp": {
                            "mode": "patch",
                            "patch_path": "tools/mcp_servers.patch.yaml",
                            "requires_trust_ack": True,
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )
    patch_file.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "target_server": "workstate-handoff-mcp",
                "ops": [
                    {
                        "op": "replace_args",
                        "value": [
                            "mcp-workstate-handoff@0.11.4",
                            "--profile",
                            "consumer",
                        ],
                    }
                ],
            },
            sort_keys=False,
        )
    )

    url, _ = fake_remote_with_generator
    ref = _plugin_own_mcp_registration(url, tmp_path)
    install(
        target=target, remote_url=url, remote_ref=ref, enforce_required_surfaces=False
    )

    patch_file.write_text(
        "schema_version: 1\ntarget_server: workstate-handoff-mcp\nops: [\n"
    )

    kinds_paths = {
        (finding["kind"], finding["path"]) for finding in doctor(target=target)
    }
    assert (
        "invalid_override_schema",
        "workstate-overrides/workstate-system/tools/mcp_servers.patch.yaml",
    ) in kinds_paths


def test_doctor_reports_hidden_override_collision_for_undeclared_skill(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    system_root = _workstate_system_root()
    if system_root is None:
        pytest.skip("packages/workstate-system not available in this environment")

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    _seed_branch_review_override(target, system_root)

    # implementation note S3: skills/ moved into the package payload tree.
    colliding_skill = next(
        path.name
        for path in (system_root / "workstate_system" / "payload" / "skills").iterdir()
        if path.is_dir() and path.name != "branch-review"
    )
    undeclared_skill = (
        target
        / "workstate-overrides"
        / "workstate-system"
        / "skills"
        / colliding_skill
        / "SKILL.md"
    )
    undeclared_skill.parent.mkdir(parents=True, exist_ok=True)
    undeclared_skill.write_text(
        f"---\nname: {colliding_skill}\ndescription: undeclared local shadow\n---\n\n"
        "This file is not declared in overrides.yaml.\n"
    )

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    kinds_paths = {
        (finding["kind"], finding["path"]) for finding in doctor(target=target)
    }
    assert (
        "hidden_override_collision",
        f"workstate-overrides/workstate-system/skills/{colliding_skill}/SKILL.md",
    ) in kinds_paths


def test_doctor_reports_pin_target_drift_for_override_aware_claude_marketplace(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    system_root = _workstate_system_root()
    if system_root is None:
        pytest.skip("packages/workstate-system not available in this environment")

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    _seed_branch_review_override(target, system_root)

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    claude_marketplace_path = target / ".claude-plugin" / "marketplace.json"
    claude_marketplace = json.loads(claude_marketplace_path.read_text())
    claude_marketplace["plugins"][0]["source"] = (
        "./.workstate/generated/plugins/workstate-system/base/claude"
    )
    claude_marketplace_path.write_text(json.dumps(claude_marketplace, indent=2) + "\n")

    kinds_paths = {
        (finding["kind"], finding["path"]) for finding in doctor(target=target)
    }
    assert ("pin_target_drift", ".claude-plugin/marketplace.json") in kinds_paths


def test_doctor_reports_plugin_source_drift_for_missing_marketplace_tree(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """A marketplace pin can point at the expected path but still be broken
    if the generated plugin tree is missing or incomplete."""
    import shutil

    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    # WORKSTATE-REF-07 always-effective: the pin targets effective/, so the missing-tree
    # drift case is a missing effective/claude tree.
    shutil.rmtree(
        target
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "effective"
        / "claude"
    )

    findings = doctor(target=target)
    source_finding = next(
        (
            finding
            for finding in findings
            if finding["kind"] == "plugin_source_drift"
            and finding["path"] == ".claude-plugin/marketplace.json"
        ),
        None,
    )
    assert source_finding is not None, findings
    assert "source path does not exist" in source_finding["message"]


def test_doctor_flags_missing_grok_plugin_tree_when_effective_exists(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """Missing ``.grok/plugins/workstate-system/`` is flagged when the
    effective grok tree is present after install."""
    import shutil

    from workstate_bootstrap.install import GROK_PLUGIN_DEST, install
    from workstate_bootstrap.subcommands import _doctor_grok_plugin_materialization

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)
    shutil.rmtree(target / GROK_PLUGIN_DEST)

    findings = _doctor_grok_plugin_materialization(target)
    assert len(findings) == 1
    assert findings[0]["kind"] == "grok_plugin_drift"
    assert findings[0]["path"] == GROK_PLUGIN_DEST.as_posix()
    assert "missing" in findings[0]["message"]


def test_doctor_passes_clean_grok_plugin_materialization(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """A freshly materialized grok plugin tree passes grok integrity checks."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import _doctor_grok_plugin_materialization

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    assert _doctor_grok_plugin_materialization(target) == []


def test_doctor_skips_grok_activation_drift_when_no_user_config(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ~/.grok/config.toml means 'grok not in use here' — doctor must not
    flag activation drift for Claude/Codex-only consumers (WORKSTATE09-R2-B-002)."""
    from unittest.mock import patch

    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import _doctor_grok_plugin_materialization

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    url, ref = fake_remote_with_generator
    with patch(
        "workstate_bootstrap.activation._grok_cli_available", return_value=False
    ):
        install(target=target, remote_url=url, remote_ref=ref)

    findings = _doctor_grok_plugin_materialization(target)
    assert not any(f["kind"] == "grok_activation_drift" for f in findings)


def test_doctor_flags_grok_activation_drift_when_bare_selector_disabled(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import _doctor_grok_plugin_materialization

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    home = tmp_path / "home"
    config = home / ".grok" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[plugins]\nenabled = []\n")
    monkeypatch.setenv("HOME", str(home))

    url, ref = fake_remote_with_generator
    from unittest.mock import patch

    with patch(
        "workstate_bootstrap.activation._grok_cli_available", return_value=False
    ):
        install(target=target, remote_url=url, remote_ref=ref)

    findings = _doctor_grok_plugin_materialization(target)
    assert any(f["kind"] == "grok_activation_drift" for f in findings)


def test_repair_restores_grok_activation_drift(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from unittest.mock import patch

    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor, repair

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    home = tmp_path / "home"
    config = home / ".grok" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[plugins]\nenabled = []\n")

    url, ref = fake_remote_with_generator

    def fake_grok_cli(args, *, cwd):
        if args[0] == "enable":
            config.write_text('[plugins]\nenabled = ["workstate-system"]\n')
        return __import__("subprocess").CompletedProcess(
            ["grok"], 0, stdout="ok", stderr=""
        )

    with patch.dict("os.environ", {"HOME": str(home)}):
        with patch(
            "workstate_bootstrap.activation._grok_cli_available", return_value=False
        ):
            install(target=target, remote_url=url, remote_ref=ref)
        assert any(f["kind"] == "grok_activation_drift" for f in doctor(target=target))
        with patch(
            "workstate_bootstrap.activation._grok_cli_available", return_value=True
        ):
            with patch(
                "workstate_bootstrap.activation._run_grok_cli",
                side_effect=fake_grok_cli,
            ):
                report = repair(target=target)
        assert any(
            entry["kind"] == "grok_activation_drift" for entry in report["repaired"]
        ), report
        assert not any(
            f["kind"] == "grok_activation_drift" for f in doctor(target=target)
        )


def test_doctor_warns_stale_grok_discovery_selector(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import _doctor_grok_plugin_materialization

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    home = tmp_path / "home"
    config = home / ".grok" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[plugins]\nenabled = ["project/a018ff86/workstate-system", "workstate-system"]\n'
    )
    monkeypatch.setenv("HOME", str(home))

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    findings = _doctor_grok_plugin_materialization(target)
    assert any(f["kind"] == "grok_stale_selector_warning" for f in findings)


def test_doctor_cli_stale_selector_warning_does_not_fail_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """severity=warning findings (stale grok selector) are reported but never
    flip the doctor exit code — WORKSTATE-REF-09 'reported, not a hard failure'
    (WORKSTATE09-R2-A-001). The doctor() result is stubbed so the exit-code gate is
    pinned in isolation from fixture-dependent drift findings."""
    from unittest.mock import patch

    from workstate_bootstrap.cli import main

    # Real grok findings carry remediation text under 'message' (older facets
    # use 'detail'); fabricate the real shape so _line's key handling is pinned
    # (WORKSTATE-REF-09-REV-A-r4-02).
    warning_finding = {
        "kind": "grok_stale_selector_warning",
        "path": "~/.grok/config.toml",
        "severity": "warning",
        "message": "stale discovery selector 'project/a018ff86/workstate-system'",
    }
    actionable_finding = {
        "kind": "grok_activation_drift",
        "path": ".grok/plugins/workstate-system",
        "message": "bare selector not enabled",
    }

    with patch("workstate_bootstrap.cli.doctor", return_value=[warning_finding]):
        exit_code = main(["doctor", "--target", str(tmp_path)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "warning grok_stale_selector_warning" in out
    assert "stale discovery selector" in out
    assert "doctor: no drift detected." in out

    # An actionable finding alongside the warning still gates exit 1.
    with patch(
        "workstate_bootstrap.cli.doctor",
        return_value=[warning_finding, actionable_finding],
    ):
        exit_code = main(["doctor", "--target", str(tmp_path)])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "grok_activation_drift" in out
    assert "bare selector not enabled" in out

    # 'detail'-keyed findings from older facets still render their text.
    with patch(
        "workstate_bootstrap.cli.doctor",
        return_value=[
            {
                "kind": "surface_drift",
                "path": ".agentic/generated",
                "detail": "symlink target missing",
            }
        ],
    ):
        exit_code = main(["doctor", "--target", str(tmp_path)])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "symlink target missing" in out


def test_doctor_flags_grok_plugin_json_mcp_servers(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """Materialized plugin.json must not declare mcpServers (compat .mcp.json)."""
    from workstate_bootstrap.install import GROK_PLUGIN_DEST, install
    from workstate_bootstrap.subcommands import _doctor_grok_plugin_materialization

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    manifest_path = target / GROK_PLUGIN_DEST / ".grok-plugin" / "plugin.json"
    payload = json.loads(manifest_path.read_text())
    payload["mcpServers"] = {"bogus": {}}
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n")

    findings = _doctor_grok_plugin_materialization(target)
    assert any(
        finding["kind"] == "grok_plugin_drift" and "mcpServers" in finding["message"]
        for finding in findings
    )


def test_doctor_flags_grok_plugin_manifest_byte_drift(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """Stale edits to materialized plugin.json are caught by byte comparison."""
    from workstate_bootstrap.install import GROK_PLUGIN_DEST, install
    from workstate_bootstrap.subcommands import _doctor_grok_plugin_materialization

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    manifest_path = target / GROK_PLUGIN_DEST / ".grok-plugin" / "plugin.json"
    manifest_path.write_text(manifest_path.read_text() + "\n")

    findings = _doctor_grok_plugin_materialization(target)
    assert any(
        finding["kind"] == "grok_plugin_drift"
        and ".grok-plugin/plugin.json differs" in finding["message"]
        for finding in findings
    )


def test_doctor_flags_grok_plugin_hooks_byte_drift(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """Stale edits to materialized hooks/hooks.json are caught by byte comparison."""
    from workstate_bootstrap.install import GROK_PLUGIN_DEST, install
    from workstate_bootstrap.subcommands import _doctor_grok_plugin_materialization

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    hooks_path = target / GROK_PLUGIN_DEST / "hooks" / "hooks.json"
    hooks_path.write_text(hooks_path.read_text() + "\n")

    findings = _doctor_grok_plugin_materialization(target)
    assert any(
        finding["kind"] == "grok_plugin_drift"
        and "hooks/hooks.json differs" in finding["message"]
        for finding in findings
    )


def test_doctor_reports_missing_codex_activation_config(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    (target / ".codex" / "config.toml").unlink()

    kinds_paths = {
        (finding["kind"], finding["path"]) for finding in doctor(target=target)
    }
    assert ("codex_activation_drift", ".codex/config.toml") in kinds_paths


def test_repair_restores_codex_activation_config(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    import tomllib

    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor, repair

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    config_path = target / ".codex" / "config.toml"
    config_path.write_text('model = "gpt-5"\n')

    assert any(f["kind"] == "codex_activation_drift" for f in doctor(target=target))

    report = repair(target=target)
    assert any(
        entry["kind"] == "codex_activation_drift" for entry in report["repaired"]
    ), report
    assert not any(f["kind"] == "codex_activation_drift" for f in doctor(target=target))

    payload = tomllib.loads(config_path.read_text())
    assert payload["model"] == "gpt-5"
    assert payload["marketplaces"]["workstate-marketplace"] == {
        "source_type": "local",
        "source": ".",
    }
    assert (
        payload["plugins"]["workstate-system@workstate-marketplace"]["enabled"] is True
    )


def test_repair_restores_codex_activation_config_with_scalar_roots(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    import tomllib

    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor, repair

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    config_path = target / ".codex" / "config.toml"
    config_path.write_text(
        'model = "gpt-5"\nmarketplaces = "not-a-table"\nplugins = "not-a-table"\n'
    )

    findings = doctor(target=target)
    activation = next(f for f in findings if f["kind"] == "codex_activation_drift")
    assert "marketplaces must be a table" in activation["message"]
    assert "plugins must be a table" in activation["message"]

    report = repair(target=target)
    assert any(
        entry["kind"] == "codex_activation_drift" for entry in report["repaired"]
    ), report
    assert not any(f["kind"] == "codex_activation_drift" for f in doctor(target=target))

    payload = tomllib.loads(config_path.read_text())
    assert payload["model"] == "gpt-5"
    assert payload["marketplaces"]["workstate-marketplace"] == {
        "source_type": "local",
        "source": ".",
    }
    assert (
        payload["plugins"]["workstate-system@workstate-marketplace"]["enabled"] is True
    )


def test_repair_restores_plugin_pin_target_drift(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor, repair

    system_root = _workstate_system_root()
    if system_root is None:
        pytest.skip("packages/workstate-system not available in this environment")

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    _seed_branch_review_override(target, system_root)

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    claude_marketplace_path = target / ".claude-plugin" / "marketplace.json"
    claude_marketplace = json.loads(claude_marketplace_path.read_text())
    claude_marketplace["plugins"][0]["source"] = (
        "./.workstate/generated/plugins/workstate-system/base/claude"
    )
    claude_marketplace_path.write_text(json.dumps(claude_marketplace, indent=2) + "\n")

    assert any(f["kind"] == "pin_target_drift" for f in doctor(target=target))

    report = repair(target=target)
    assert any(entry["kind"] == "pin_target_drift" for entry in report["repaired"]), (
        report
    )
    assert not any(f["kind"] == "pin_target_drift" for f in doctor(target=target))


def test_stale_override_detection_uses_plugin_lock_not_stderr(tmp_path: Path) -> None:
    from workstate_bootstrap.subcommands import (
        _plugin_check_reports_only_stale_override,
    )

    lock_dir = (
        tmp_path
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "effective"
    )
    lock_dir.mkdir(parents=True)
    (lock_dir / "plugin-lock.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "base_remote_sha": "a" * 40,
                "effective_root": ".",
                "components": [
                    {
                        "component_kind": "skill",
                        "name": "branch-review",
                        "mode": "replace",
                        "effective_digest": "sha256:" + "b" * 64,
                        "status": "stale",
                        "override_path": "skills/branch-review/SKILL.md",
                        "recorded_upstream_digest": "sha256:" + "c" * 64,
                        "current_base_digest": "sha256:" + "d" * 64,
                    }
                ],
            }
        )
    )

    assert _plugin_check_reports_only_stale_override(
        lock_dir, "warning: preserved local override"
    )


def test_doctor_and_repair_use_recorded_plugin_override_root(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    import shutil

    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor, repair

    system_root = _workstate_system_root()
    if system_root is None:
        pytest.skip("packages/workstate-system not available in this environment")

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    override_root = target / "custom-overrides" / "workstate-system"
    _seed_branch_review_override_at(override_root, system_root)
    url, ref = fake_remote_with_generator

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        plugin_overrides=override_root,
        enforce_required_surfaces=False,
    )

    effective_root = (
        target
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "effective"
    )
    shutil.rmtree(effective_root)

    findings = doctor(target=target)
    assert any(
        finding["path"] == ".workstate/generated/plugins/workstate-system/effective"
        for finding in findings
    )

    report = repair(target=target)
    assert any(
        entry["path"] == ".workstate/generated/plugins/workstate-system/effective"
        for entry in report["repaired"]
    ), report
    assert (effective_root / "plugin-lock.json").is_file()


def test_update_requires_required_surfaces_by_default(
    tmp_path: Path, fake_remote_with_two_refs: tuple[str, str, str]
) -> None:
    """PLAN0003-S4-UP-002: update must refuse refs missing scripts/hooks
    unless the caller explicitly opts out."""
    from workstate_bootstrap.install import BootstrapManifestValidationError, install
    from workstate_bootstrap.subcommands import update

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref_old, ref_new = fake_remote_with_two_refs

    install(
        target=target,
        remote_url=url,
        remote_ref=ref_old,
        enforce_required_surfaces=False,
    )

    with pytest.raises(BootstrapManifestValidationError):
        update(target=target, remote_ref=ref_new)


def test_update_reset_overrides_forwards_to_install(
    tmp_path: Path, fake_remote_with_generator_two_refs: tuple[str, str, str]
) -> None:
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import update

    system_root = _workstate_system_root()
    if system_root is None:
        pytest.skip("packages/workstate-system not available in this environment")

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    _seed_branch_review_override(target, system_root)
    url, ref_old, ref_new = fake_remote_with_generator_two_refs

    install(
        target=target,
        remote_url=url,
        remote_ref=ref_old,
        enforce_required_surfaces=False,
    )
    _git("add", "-A", cwd=target)
    _git("commit", "-m", "baseline override install", cwd=target)

    manifest = update(
        target=target,
        remote_ref=ref_new,
        reset_overrides=True,
        enforce_required_surfaces=False,
    )

    assert not (target / "workstate-overrides" / "workstate-system").exists()
    assert manifest.get("plugin_overrides_path") is None


def test_update_console_script(
    tmp_path: Path, fake_remote_with_two_refs: tuple[str, str, str]
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref_old, ref_new = fake_remote_with_two_refs

    install(target=target, remote_url=url, remote_ref=ref_old)
    sha_before = json.loads((target / ".workstate-bootstrap.json").read_text())[
        "remote_sha"
    ]

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "workstate_bootstrap",
            "update",
            "--target",
            str(target),
            "--remote-ref",
            ref_new,
            "--no-enforce-required-surfaces",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    sha_after = json.loads((target / ".workstate-bootstrap.json").read_text())[
        "remote_sha"
    ]
    assert sha_after != sha_before


def test_update_preserves_profile_all_lifecycle_hoist(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WORKSTATE-REF-48: ``workstate-bootstrap update`` (which delegates to
    ``install(profile=PROFILE_ALL, ...)``) must preserve the lifecycle
    hoist on the consumer.

    The production fix lives entirely in ``install.py`` — ``update()``
    inherits it via line ``subcommands.py:486``'s install delegation.
    This explicit assertion exists so a future refactor of ``update()``
    that bypasses ``install()`` (e.g. to skip surfaces for speed) cannot
    silently regress the epic-level drift contract.
    """
    from workstate_bootstrap.install import (
        LIFECYCLE_INCLUDE_SENTINEL_BEGIN,
        install,
    )
    from workstate_bootstrap.subcommands import update

    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake-home"))
    (tmp_path / "fake-home").mkdir()

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_generator

    install(target=target, remote_url=url, remote_ref=ref, profile="all")
    update(target=target, remote_ref=ref, enforce_required_surfaces=False)

    assert (target / "Makefile.d" / "lifecycle.mk").is_file(), (
        "after install(--profile all) followed by update(), the lifecycle "
        "fragment must remain materialized in the consumer"
    )
    assert (target / "scripts" / "workstate" / "lifecycle" / "__init__.py").is_file()

    makefile_text = (target / "Makefile").read_text()
    assert makefile_text.count(LIFECYCLE_INCLUDE_SENTINEL_BEGIN) == 1, (
        "update() must not duplicate the sentinel-bracketed include block; "
        "exactly one LIFECYCLE_INCLUDE_SENTINEL_BEGIN marker is the "
        "post-fix invariant for the install->update sequence"
    )


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------


def test_repair_restores_missing_surface_symlink(
    tmp_path: Path, fake_remote_with_surfaces
) -> None:
    """A clean install whose `scripts/hooks` symlink was deleted entirely
    should be restored by `repair` without --force-dirty."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor, repair

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces
    install(target=target, remote_url=url, remote_ref=ref)

    surface = target / "scripts/hooks"
    surface.unlink()
    assert any(f["kind"] == "surface_drift" for f in doctor(target=target))

    report = repair(target=target)
    assert report["repaired"], report
    assert surface.is_symlink()
    assert doctor(target=target) == []


def test_repair_refuses_real_directory_without_force(
    tmp_path: Path, fake_remote_with_surfaces
) -> None:
    """A real non-empty directory at a managed surface is treated as user
    content and must NOT be silently overwritten without --force-dirty."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import repair

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces
    install(target=target, remote_url=url, remote_ref=ref)

    surface = target / "scripts/hooks"
    surface.unlink()
    surface.mkdir()
    (surface / "user-content.txt").write_text("important local notes\n")

    report = repair(target=target)
    assert any(s["path"] == "scripts/hooks" for s in report["skipped"]), report
    assert (surface / "user-content.txt").exists()
    assert not surface.is_symlink()


def test_repair_force_dirty_overwrites_real_directory(
    tmp_path: Path, fake_remote_with_surfaces
) -> None:
    """With --force-dirty the dirty surface is replaced with the canonical
    symlink (rg-017: never silently force; here it's explicit)."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor, repair

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces
    install(target=target, remote_url=url, remote_ref=ref)

    surface = target / "scripts/hooks"
    surface.unlink()
    surface.mkdir()
    (surface / "user-content.txt").write_text("user data lost on force\n")

    report = repair(target=target, force_dirty=True)
    assert "scripts/hooks" in [s["path"] for s in report["repaired"]], report
    assert surface.is_symlink()
    assert doctor(target=target) == []


def test_repair_console_script_force_dirty_flag(
    tmp_path: Path, fake_remote_with_surfaces
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces
    install(target=target, remote_url=url, remote_ref=ref)

    surface = target / "scripts/hooks"
    surface.unlink()
    surface.mkdir()
    (surface / "user-content.txt").write_text("x\n")

    # Without --force-dirty: refuses, exit 0 (skipped is not a failure).
    refused = subprocess.run(
        [
            sys.executable,
            "-m",
            "workstate_bootstrap",
            "repair",
            "--target",
            str(target),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert refused.returncode == 0, refused.stderr
    assert "skipped" in refused.stdout.lower()
    assert not surface.is_symlink()

    # With --force-dirty: replaces.
    forced = subprocess.run(
        [
            sys.executable,
            "-m",
            "workstate_bootstrap",
            "repair",
            "--target",
            str(target),
            "--force-dirty",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert forced.returncode == 0, forced.stderr
    assert surface.is_symlink()


# ---------------------------------------------------------------------------
# CLI --mcp-servers flag plumbing
# ---------------------------------------------------------------------------


def test_cli_install_with_mcp_servers_flag_writes_configs(
    tmp_path: Path, fake_remote_with_surfaces
) -> None:
    """`workstate-bootstrap install --mcp-servers <file.json>` reads the JSON
    file and writes managed MCP entries into .mcp.json + .vscode/mcp.json."""
    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces

    spec_file = tmp_path / "servers.json"
    spec_file.write_text(json.dumps(SAMPLE_MCP_SERVERS))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "workstate_bootstrap",
            "install",
            "--target",
            str(target),
            "--remote-url",
            url,
            "--remote-ref",
            ref,
            "--mcp-servers",
            str(spec_file),
            # MCP-config writers run only under ``all``. Passed
            # explicitly here to pin the test intent — ``all`` is also
            # the CLI default after WORKSTATE-REF-56 implementation note, but we keep the
            # flag to make the requirement obvious.
            "--profile",
            "all",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr

    mcp_doc = json.loads((target / ".mcp.json").read_text())
    assert (
        mcp_doc["mcpServers"]["workstate-handoff-mcp"]
        == SAMPLE_MCP_SERVERS["workstate-handoff-mcp"]
    )
    vscode_doc = json.loads((target / ".vscode/mcp.json").read_text())
    assert (
        vscode_doc["servers"]["workstate-handoff-mcp"]
        == SAMPLE_MCP_SERVERS["workstate-handoff-mcp"]
    )


def test_cli_doctor_with_mcp_servers_flag_detects_drift(
    tmp_path: Path, fake_remote_with_surfaces
) -> None:
    """`doctor --mcp-servers <file>` flags config_drift when an installed
    managed MCP entry has been removed from .mcp.json."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    # Tamper with .mcp.json by removing the managed entry.
    mcp_path = target / ".mcp.json"
    doc = json.loads(mcp_path.read_text())
    del doc["mcpServers"]["workstate-handoff-mcp"]
    mcp_path.write_text(json.dumps(doc))

    spec_file = tmp_path / "servers.json"
    spec_file.write_text(json.dumps(SAMPLE_MCP_SERVERS))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "workstate_bootstrap",
            "doctor",
            "--target",
            str(target),
            "--mcp-servers",
            str(spec_file),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1, result.stdout
    assert "config_drift: .mcp.json" in result.stdout


def test_cli_repair_with_mcp_servers_flag_restores_config(
    tmp_path: Path, fake_remote_with_surfaces
) -> None:
    """`repair --mcp-servers <file>` re-writes .mcp.json so the managed
    entry is present again."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    mcp_path = target / ".mcp.json"
    doc = json.loads(mcp_path.read_text())
    del doc["mcpServers"]["workstate-handoff-mcp"]
    mcp_path.write_text(json.dumps(doc))

    spec_file = tmp_path / "servers.json"
    spec_file.write_text(json.dumps(SAMPLE_MCP_SERVERS))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "workstate_bootstrap",
            "repair",
            "--target",
            str(target),
            "--mcp-servers",
            str(spec_file),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    fixed = json.loads(mcp_path.read_text())
    assert (
        fixed["mcpServers"]["workstate-handoff-mcp"]
        == SAMPLE_MCP_SERVERS["workstate-handoff-mcp"]
    )


def test_update_rejects_package_source_install(tmp_path: Path) -> None:
    """implementation note post-merge audit follow-up: a package-source install manifest
    omits remote_url (source_kind=package copies from the installed
    distribution — there is no remote to refresh). update() must reject it
    with a clear message instead of raising KeyError on manifest['remote_url']."""
    from workstate_bootstrap.install import BOOTSTRAP_MANIFEST_NAME, SCHEMA_VERSION
    from workstate_bootstrap.subcommands import update

    target = tmp_path / "consumer"
    target.mkdir()
    (target / BOOTSTRAP_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "source_kind": "package",
                "package_version": "0.1.0",
                "surfaces": [],
                "configs": [],
            }
        )
    )

    with pytest.raises(ValueError, match="package"):
        update(target=target, remote_ref="main")
