"""End-to-end rehearsal: a single ``install(... mcp_servers="default")`` call
must produce the surface invariants implementation note step 2 commits to.

Captures the manual rehearsal scenario from
``docs/plans/0002-distribution-and-external-repo-readiness.md`` step 2c as a
test so future regressions surface in CI before they reach external repos.

Invariants asserted here:

1. ``GENERATED_SURFACES`` entries are populated and contain no broken links.
2. ``.mcp.json``, ``.vscode/mcp.json``, and ``.codex/config.toml`` register
   both managed MCP servers (``workstate-handoff-mcp`` and
   ``workstate-orchestrator-mcp``) with runnable ``uvx`` command lines.
3. ``core.hooksPath`` is set to ``scripts/hooks/git`` and that directory
   contains every standard git hook script the monorepo ships
   (``post-checkout``, ``post-commit``, ``post-merge``, ``post-rewrite``,
   ``pre-commit``, ``pre-push``), each executable. The implementation note implementation note
   regression target: pointing ``core.hooksPath`` at the parent
   ``scripts/hooks/`` directory makes git silently resolve nothing
   because the hook files live one level down. implementation note implementation note adds
   ``pre-commit`` to the pinned surface and asserts that the Python
   helper(s) the hooks delegate to (``check_branch_naming.py``) are
   materialized alongside the hook scripts.
4. The overlay manifest enumerates shared surfaces, carved shared children,
   and generated surfaces with the right ``source`` discriminator.

The MCP-server-boots check from the plan (``uvx mcp-workstate-handoff --help``)
is a network-dependent integration check that lives outside the unit-test
loop; it is rehearsed manually before each release.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import tomllib

from tests.test_install import (  # noqa: F401 — re-exported pytest fixture
    SHARED_GIT_HOOK_NAMES,
    SHARED_HOOK_HELPER_NAMES,
    _install_fake_uvx,
    _git,
    fake_remote_with_generator,
)


def _init_git_repo(path: Path) -> None:
    _git("init", "--initial-branch=main", cwd=path)
    _git("config", "user.email", "rehearsal@example.com", cwd=path)
    _git("config", "user.name", "Rehearsal", cwd=path)


def test_install_with_default_servers_satisfies_rehearsal_invariants(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import (
        DEFAULT_MCP_SERVERS,
        GENERATED_SURFACES,
        SHARED_SURFACES,
        install,
    )

    target = tmp_path / "rehearsal-target"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_generator

    _install_fake_uvx(monkeypatch, tmp_path)

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers="default",
    )

    # 1. Generated surfaces populated; no broken links anywhere under them.
    for surface in GENERATED_SURFACES:
        path = target / surface
        assert path.is_dir() and not path.is_symlink(), surface
        produced = list(path.rglob("*"))
        assert produced, f"{surface} must contain generator output"
        for entry in produced:
            if entry.is_symlink():
                resolved = entry.resolve(strict=False)
                assert resolved.exists(), f"broken link inside {surface}: {entry}"

    # 2. All three MCP-config surfaces register both managed servers with
    #    a runnable MCP stdio command line.
    expected_servers = set(DEFAULT_MCP_SERVERS)
    assert expected_servers == {"workstate-handoff-mcp", "workstate-orchestrator-mcp"}

    mcp_doc = json.loads((target / ".mcp.json").read_text())
    for name in expected_servers:
        entry = mcp_doc["mcpServers"][name]
        assert entry["command"] == "uvx", entry
        assert "--workspace-root" in entry["args"], entry
        assert entry["args"][-1] == "serve-stdio", entry

    vscode_doc = json.loads((target / ".vscode" / "mcp.json").read_text())
    for name in expected_servers:
        entry = vscode_doc["servers"][name]
        assert entry["command"] == "uvx", entry
        assert "--workspace-root" in entry["args"], entry
        assert entry["args"][-1] == "serve-stdio", entry

    codex_doc = tomllib.loads((target / ".codex" / "config.toml").read_text())
    for name in expected_servers:
        entry = codex_doc["mcp_servers"][name]
        assert entry["command"] == "uvx", entry
        assert "--workspace-root" in entry["args"], entry
        assert entry["args"][-1] == "serve-stdio", entry

    # 3. ``core.hooksPath`` resolves to a directory carrying every
    #    standard git hook script the monorepo ships, each executable.
    #    implementation note implementation note regression: the resolved path AND the on-disk
    #    layout must agree, otherwise git silently runs no hook.
    hooks_path = _git("config", "--get", "core.hooksPath", cwd=target)
    assert hooks_path == "scripts/hooks/git"
    hooks_dir = (target / hooks_path).resolve()
    assert hooks_dir.is_dir(), hooks_dir
    for name in SHARED_GIT_HOOK_NAMES:
        hook_path = hooks_dir / name
        assert hook_path.is_file(), f"missing git hook {name} at {hook_path}"
        assert os.access(hook_path, os.X_OK), f"git hook {name} not executable"

    # implementation note implementation note: helper scripts the git hooks delegate to MUST
    # be materialized in the parent ``scripts/hooks/`` directory. The
    # post-checkout / pre-commit / pre-push gates ``exec`` these helpers
    # — silently absent helpers would let non-conforming branches slip
    # through every gate without ever running the validator.
    helpers_dir = (target / "scripts" / "hooks").resolve()
    for helper in SHARED_HOOK_HELPER_NAMES:
        helper_path = helpers_dir / helper
        assert helper_path.is_file(), (
            f"missing hook helper {helper} at {helper_path}"
        )

    # 4. Manifest enumerates all SHARED+GENERATED surfaces with the right
    #    source discriminator. ``shared`` entries come from the remote;
    #    ``generated`` entries are written by the generator into the target.
    #    WS-REBRAND-01 Phase A: surfaces in SURFACE_CHILD_EXCLUSIONS are
    #    carved (real dir + per-child symlinks), so they appear as per-child
    #    ``shared`` entries rather than a single bare-parent entry.
    from workstate_bootstrap.install import SURFACE_CHILD_EXCLUSIONS

    by_path = {entry["path"]: entry for entry in manifest["surfaces"]}
    for surface in SHARED_SURFACES:
        if surface in SURFACE_CHILD_EXCLUSIONS:
            assert surface not in by_path, (
                f"carved {surface} must not have a bare parent manifest entry"
            )
            child_entries = [
                p for p, e in by_path.items()
                if p.startswith(f"{surface}/") and e["source"] == "shared"
            ]
            assert child_entries, (
                f"carved {surface} must contribute at least one per-child "
                f"shared entry; got {sorted(by_path)}"
            )
            continue
        assert by_path[surface]["source"] == "shared", by_path[surface]
    for surface in GENERATED_SURFACES:
        assert by_path[surface]["source"] == "generated", by_path[surface]

    config_paths = {entry["path"] for entry in manifest["configs"]}
    assert {
        ".mcp.json",
        ".vscode/mcp.json",
        ".codex/config.toml",
        "core.hooksPath",
    } <= config_paths
    # WORKSTATE-REF-56 implementation note: the harness Stop hook remains opt-in. The shared
    # ``.claude/settings.json`` file is now written for the project-scoped
    # plugin marketplace pin, but the user-owned local settings file is
    # still untouched unless ``--install-claude-stop-hook-local`` is set.
    assert ".claude-plugin/marketplace.json" in config_paths
    assert ".claude/settings.json" in config_paths
    assert ".agents/plugins/marketplace.json" in config_paths
    assert ".claude/settings.local.json" not in config_paths
    assert not (target / ".claude" / "settings.local.json").exists()


def test_install_with_default_servers_is_idempotent(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeat ``install`` with the default server map must converge: same
    config surfaces, same hooksPath, no duplicated server entries."""
    from workstate_bootstrap.install import install

    target = tmp_path / "rehearsal-target"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_generator

    _install_fake_uvx(monkeypatch, tmp_path)

    install(target=target, remote_url=url, remote_ref=ref, mcp_servers="default")
    first = (target / ".mcp.json").read_text()

    install(target=target, remote_url=url, remote_ref=ref, mcp_servers="default")
    second = (target / ".mcp.json").read_text()

    # Server entries already match what install would write -> file content
    # stable on rerun (modulo formatter-equivalent whitespace).
    assert json.loads(first) == json.loads(second)
    # Hooks configuration survives the rerun.
    hooks_path = subprocess.check_output(
        ["git", "config", "--get", "core.hooksPath"], cwd=target, text=True
    ).strip()
    assert hooks_path == "scripts/hooks/git"


def test_install_with_default_servers_requires_uvx_in_args() -> None:
    """Sanity: the built-in default map must not be empty and each entry
    must carry a non-empty ``args`` list. Cheap regression guard for the
    map declaration itself — failing here is louder than failing inside
    a slow end-to-end install."""
    from workstate_bootstrap.install import DEFAULT_MCP_SERVERS

    assert set(DEFAULT_MCP_SERVERS) == {
        "workstate-handoff-mcp",
        "workstate-orchestrator-mcp",
    }
    for name, entry in DEFAULT_MCP_SERVERS.items():
        assert entry["type"] == "stdio", (name, entry)
        assert entry["command"] == "uvx", (name, entry)
        assert "--workspace-root" in entry["args"], (name, entry)
        assert entry["args"][-1] == "serve-stdio", (name, entry)
    assert DEFAULT_MCP_SERVERS["workstate-handoff-mcp"]["args"][0] == "mcp-workstate-handoff@0.12.1"
    assert DEFAULT_MCP_SERVERS["workstate-orchestrator-mcp"]["args"][0] == "mcp-workstate-orchestrator@0.5.2"


def test_rehearsal_hoists_plan_targets_makefile_and_shell_wrapper(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """implementation note implementation note: the bootstrap installer must hoist
    ``Makefile.d/plans.mk`` and ``scripts/workstate/git-plan-cat.sh`` into
    the consumer worktree so Slices 2-4 have a real surface to extend.

    Asserts file presence on disk under the consumer root, executable bit
    on the shell wrapper, and that ``Makefile.d/plans.mk`` declares the
    canonical launcher token
    ``WORKSTATE_HANDOFF_PLAN_CLI ?= uvx --from mcp-workstate-handoff python -m
    workstate_handoff_mcp.plan_cli`` so the launcher contract implementation note ships
    is locked in at hoist time. The end-to-end ``make plan-show`` gate
    lives in implementation note's own rehearsal test; this test only verifies the
    files land.
    """
    from workstate_bootstrap.install import install

    target = tmp_path / "rehearsal-target"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_generator

    _install_fake_uvx(monkeypatch, tmp_path)

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers="default",
    )

    plans_mk = target / "Makefile.d" / "plans.mk"
    assert plans_mk.exists(), (
        "Makefile.d/plans.mk must land under the consumer root after install "
        "so Slices 2-4 can extend the plan-targets surface"
    )
    plans_mk_text = plans_mk.read_text()
    assert (
        "WORKSTATE_HANDOFF_PLAN_CLI ?= uvx --from mcp-workstate-handoff python -m workstate_handoff_mcp.plan_cli"
        in plans_mk_text
    ), (
        "Makefile.d/plans.mk must declare the canonical launcher token "
        "(WORKSTATE_HANDOFF_PLAN_CLI) so the launcher contract is locked in at "
        f"hoist time. Got:\n{plans_mk_text}"
    )

    # WORKSTATE-REF-69 implementation note: every plan-targets recipe must forward
    # ``--workspace-root $(CURDIR)`` so a coordinator on `main` does not
    # need to export ``WORKSTATE_HANDOFF_WORKSPACE_ROOT`` to use the plan
    # surface. Asserted at the bootstrap layer because the same Makefile
    # fragment ships to every consumer; missing the token here would
    # silently regress every downstream install.
    workspace_root_lines = [
        line
        for line in plans_mk_text.splitlines()
        if "$(WORKSTATE_HANDOFF_PLAN_CLI)" in line
    ]
    assert workspace_root_lines, (
        "Makefile.d/plans.mk must invoke $(WORKSTATE_HANDOFF_PLAN_CLI) on at "
        f"least one recipe line; got:\n{plans_mk_text}"
    )
    for line in workspace_root_lines:
        assert "--workspace-root $(CURDIR)" in line, (
            "Each plan-targets recipe must forward --workspace-root "
            f"$(CURDIR) to the launcher; offending line:\n{line}"
        )

    git_plan_cat = target / "scripts" / "workstate" / "git-plan-cat.sh"
    assert git_plan_cat.exists(), (
        "scripts/workstate/git-plan-cat.sh must land under the consumer root"
    )
    # Executable bit follows the source file's mode through the symlink.
    resolved = git_plan_cat.resolve()
    assert resolved.exists(), (
        f"git-plan-cat.sh resolves to a missing target: {resolved}"
    )
    assert os.access(resolved, os.X_OK), (
        f"git-plan-cat.sh must be executable; mode={oct(resolved.stat().st_mode)}"
    )

    # No workstate_handoff_mcp Python module is hoisted via overlay — Python lives
    # in the installable package, not in the file copy. Belt-and-braces guard
    # against a future drift where someone tries to ship the Python via overlay.
    assert not (target / "scripts" / "workstate" / "plan_resolve.py").exists()
    assert not (target / "workstate_handoff_mcp").exists()

    # WS-REBRAND-01 Phase A: the evals harness is carved out of the consumer
    # surface — scripts/workstate and Makefile.d materialize as real directories
    # whose evals children are fully absent.
    assert not (target / "scripts" / "workstate" / "evals").exists(), (
        "scripts/workstate/evals must be carved out of the consumer tree"
    )
    assert not (target / "Makefile.d" / "evals.mk").exists(), (
        "Makefile.d/evals.mk must be carved out of the consumer tree"
    )
    assert (target / "scripts" / "workstate").is_dir()
    assert not (target / "scripts" / "workstate").is_symlink()

    # Manifest records the carved surfaces' non-excluded children with
    # source='shared' (per-child entries, not a bare parent symlink).
    by_path = {entry["path"]: entry for entry in manifest["surfaces"]}
    assert by_path["scripts/workstate/git-plan-cat.sh"]["source"] == "shared", (
        by_path.get("scripts/workstate/git-plan-cat.sh")
    )
    assert by_path["Makefile.d/plans.mk"]["source"] == "shared", (
        by_path.get("Makefile.d/plans.mk")
    )
    assert "scripts/workstate" not in by_path, (
        "carved scripts/workstate must not have a bare parent manifest entry"
    )
    assert "Makefile.d" not in by_path


def test_rehearsal_make_plan_show_runs_via_uvx_launcher(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """implementation note implementation note (PR-04 launcher contract gate): a freshly
    bootstrapped consumer can run ``make plan-show`` with no
    ``pip install workstate-handoff-mcp`` step. The recipe shells through
    the canonical ``WORKSTATE_HANDOFF_PLAN_CLI`` token (``uvx --from
    mcp-workstate-handoff python -m workstate_handoff_mcp.plan_cli``) which the
    fake-uvx shim resolves back to this monorepo's package source.

    Asserts:
      a) ``make plan-show TASK=<ref>`` exits 0 against a seeded handoff DB;
      b) stdout matches ``git show <branch>:<rel_path>``;
      c) the resolved launcher value is the canonical uvx form, proving
         the recipe did not fall back to a bare ``python -m``.
    """
    pytest.importorskip("workstate_handoff_mcp")
    if shutil.which("make") is None:
        pytest.skip("make not on PATH")

    from workstate_bootstrap.install import install

    target = tmp_path / "rehearsal-target"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_generator

    _install_fake_uvx(monkeypatch, tmp_path)

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers="default",
    )

    # The bootstrap installer hoists Makefile.d/plans.mk but does not
    # write a root Makefile (each consumer wires its own). For the
    # launcher-contract test, drop a one-line root Makefile that
    # includes the hoisted plans.mk so `make plan-show` resolves.
    (target / "Makefile").write_text("include Makefile.d/*.mk\n")

    # Commit the install output so `main` is born and the feature branch
    # forks from a real commit. Without this, `git checkout main` after
    # branching is "unborn branch" and fails.
    _git("add", "-A", cwd=target)
    _git("commit", "-m", "rehearsal: bootstrap install", cwd=target)

    # Seed: commit a plan file on a feature branch, then register a
    # handoff row pointing at it. Returns to main so HEAD is unchanged
    # when `make plan-show` runs.
    branch = "feature/WORKSTATE-99-rehearsal"
    rel = "docs/plans/0099-rehearsal.md"
    body = "# rehearsal plan\n\ncontent.\n"
    _git("checkout", "-b", branch, cwd=target)
    (target / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (target / rel).write_text(body)
    _git("add", rel, cwd=target)
    _git("commit", "-m", "add rehearsal plan", cwd=target)
    _git("checkout", "main", cwd=target)

    state_dir = target / ".task-state"
    state_dir.mkdir(exist_ok=True)
    from workstate_handoff_mcp import api as mcp_server
    from workstate_handoff_mcp.config import RuntimeConfig

    runtime = RuntimeConfig.for_workspace(
        target,
        state_dir=state_dir,
        current_task_path=target / "CURRENT_TASK.json",
        dashboard_path=target / "DASHBOARD.txt",
    )
    mcp_server.configure_runtime(runtime)
    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-99",
        objective="rehearsal launcher gate",
        status="in_progress",
        target_branch=branch,
        task_plan_path=rel,
    )
    mcp_server.reset_runtime_config()

    # The consumer worktree has no `workstate_handoff_mcp` on PYTHONPATH —
    # the fake uvx shim is the only path to the package.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["WORKSTATE_HANDOFF_WORKSPACE_ROOT"] = str(target)

    expected = subprocess.run(
        ["git", "show", f"{branch}:{rel}"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    proc = subprocess.run(
        ["make", "plan-show", "TASK=WORKSTATE-REF-99"],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == expected, (proc.stdout, expected)

    # Lock the launcher token: the recipe must resolve through uvx, not
    # a bypass. `make -n` prints the resolved command without running it.
    dryrun = subprocess.run(
        ["make", "-n", "plan-show", "TASK=WORKSTATE-REF-99"],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
    )
    assert dryrun.returncode == 0, dryrun.stderr
    assert (
        "uvx --from mcp-workstate-handoff python -m workstate_handoff_mcp.plan_cli"
        in dryrun.stdout
    ), dryrun.stdout
