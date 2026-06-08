"""implementation note: worktree install source + dogfood/overlay coherence slices."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from tests.test_install import fake_remote_with_generator  # noqa: F401


def _install_module():
    return importlib.import_module("workstate_bootstrap.install")


def _stub_heavy_install_steps(
    install_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_module, "_run_init_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(install_module, "_run_generator", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        install_module, "_prepare_plugin_generated_surfaces", lambda *args, **kwargs: []
    )


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=30,
    )
    return result.stdout.strip()


def _init_git_repo(path: Path) -> None:
    _git("init", "--initial-branch=main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def _seed_worktree_repo(tmp_path: Path, fake_remote_with_generator) -> Path:
    url, ref = fake_remote_with_generator
    repo = tmp_path / "worktree-repo"
    subprocess.run(
        ["git", "clone", url, str(repo)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    _git("checkout", ref, cwd=repo)
    _git("commit", "--allow-empty", "-m", "seed", cwd=repo)
    return repo


def test_install_from_worktree_symlinks_surfaces_and_records_manifest(
    tmp_path: Path, fake_remote_with_generator, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_module = _install_module()
    repo = _seed_worktree_repo(tmp_path, fake_remote_with_generator)
    _stub_heavy_install_steps(install_module, monkeypatch)

    manifest = install_module.install(
        target=repo,
        source="worktree",
        mcp_servers=None,
        enforce_required_surfaces=False,
    )

    hooks = repo / "scripts" / "hooks"
    assert hooks.is_symlink(), "worktree source must symlink shared surfaces"
    # PD-01: the symlink must resolve to a real target inside the worktree —
    # not dangle, and not be self-referential/circular (the self-host risk).
    assert hooks.exists(), "worktree symlink must resolve to a real target"
    assert hooks.resolve().is_relative_to(repo.resolve()), (
        "worktree symlink must point inside the worktree (not circular)"
    )
    assert not (repo / ".workstate" / "remote").exists(), (
        "worktree source must not create a managed clone"
    )
    assert manifest["source_kind"] == "worktree"
    assert manifest.get("remote_sha") == _git("rev-parse", "HEAD", cwd=repo)
    on_disk = json.loads((repo / ".workstate-bootstrap.json").read_text())
    assert on_disk["source_kind"] == "worktree"


def test_worktree_install_passes_none_expected_remote_url_to_init_state(
    tmp_path: Path, fake_remote_with_generator, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_module = _install_module()
    repo = _seed_worktree_repo(tmp_path, fake_remote_with_generator)
    _stub_heavy_install_steps(install_module, monkeypatch)
    seen: list[str | None] = []

    def capture_init(
        target: Path, mcp_servers, expected_remote_url: str | None = None
    ) -> None:
        seen.append(expected_remote_url)

    monkeypatch.setattr(install_module, "_run_init_state", capture_init)

    install_module.install(
        target=repo,
        source="worktree",
        mcp_servers={"workstate-handoff-mcp": {"command": "uv", "args": ["run"]}},
        enforce_required_surfaces=False,
    )
    assert seen == [None]


def test_git_overlay_install_defaults_remote_url_from_existing_clone(
    tmp_path: Path, fake_remote_with_generator
) -> None:
    from workstate_bootstrap.install import _resolve_git_overlay_remote_url

    url, _ref = fake_remote_with_generator
    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    clone = target / ".workstate" / "remote"
    subprocess.run(["git", "clone", url, str(clone)], check=True, capture_output=True)
    custom_origin = "git@example.invalid:custom/workstate.git"
    _git("remote", "set-url", "origin", custom_origin, cwd=clone)

    assert _resolve_git_overlay_remote_url(target, None) == custom_origin
    assert (
        _resolve_git_overlay_remote_url(
            target, "git@example.invalid:other/repo.git"
        )
        == "git@example.invalid:other/repo.git"
    )


def test_update_worktree_manifest_refreshes_head(
    tmp_path: Path, fake_remote_with_generator, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_module = _install_module()
    from workstate_bootstrap.subcommands import update

    repo = _seed_worktree_repo(tmp_path, fake_remote_with_generator)
    _stub_heavy_install_steps(install_module, monkeypatch)
    first = install_module.install(
        target=repo,
        source="worktree",
        mcp_servers=None,
        enforce_required_surfaces=False,
    )
    _git("commit", "--allow-empty", "-m", "bump", cwd=repo)
    refreshed = update(target=repo, mcp_servers=None, enforce_required_surfaces=False)
    assert refreshed["source_kind"] == "worktree"
    assert refreshed["remote_sha"] != first["remote_sha"]
    assert refreshed["remote_sha"] == _git("rev-parse", "HEAD", cwd=repo)


def test_doctor_reports_source_kind_drift_for_package_manifest_with_clone(
    tmp_path: Path,
) -> None:
    from workstate_bootstrap.subcommands import doctor

    target = tmp_path / "consumer"
    target.mkdir()
    clone = target / ".workstate" / "remote"
    clone.mkdir(parents=True)
    (clone / ".git").mkdir()
    hooks = target / "scripts" / "hooks"
    hooks.parent.mkdir(parents=True, exist_ok=True)
    hooks.symlink_to(clone / "scripts" / "hooks")
    (target / ".workstate-bootstrap.json").write_text(
        json.dumps(
            {
                "schema_version": 5,
                "source_kind": "package",
                "package_version": "0.2.4",
                "profile": "all",
                "surfaces": [{"path": "scripts/hooks", "source": "shared"}],
                "configs": [],
                "mcp_servers": [],
            }
        )
        + "\n"
    )

    findings = doctor(target=target, mcp_servers=None)
    assert any(f.get("kind") == "source_kind_drift" for f in findings)


def test_apply_hooks_records_managed_adapter_without_source_resolution(
    tmp_path: Path, fake_remote_with_generator, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_module = _install_module()
    from workstate_bootstrap.subcommands import apply_hooks

    repo = _seed_worktree_repo(tmp_path, fake_remote_with_generator)
    _stub_heavy_install_steps(install_module, monkeypatch)
    install_module.install(
        target=repo,
        source="worktree",
        mcp_servers=None,
        enforce_required_surfaces=False,
    )

    manifest = apply_hooks(
        target=repo,
        install_claude_error_hook=True,
    )
    rows = [
        entry
        for entry in manifest.get("configs", [])
        if isinstance(entry, dict) and entry.get("kind") == "hook_adapter"
    ]
    assert any(
        row.get("opt_in_flag") == "--install-claude-error-hook" for row in rows
    )


def test_worktree_install_primes_adjacent_manifest_before_init_state(
    tmp_path: Path, fake_remote_with_generator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PD-02: a worktree install over a pre-existing handoff.db with no prior
    manifest must write the adjacent manifest BEFORE init-state, so the
    ForeignStateReuseError guard (a) sees an honest manifest and no-ops."""
    install_module = _install_module()
    repo = _seed_worktree_repo(tmp_path, fake_remote_with_generator)
    _stub_heavy_install_steps(install_module, monkeypatch)

    # The trip case: a pre-existing DB and NO adjacent manifest yet.
    state_dir = repo / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "handoff.db").write_bytes(b"")
    manifest_path = repo / ".workstate-bootstrap.json"
    if manifest_path.exists():
        manifest_path.unlink()

    observed: list[dict[str, object]] = []

    def spy_init(
        target: Path, mcp_servers, expected_remote_url: str | None = None
    ) -> None:
        adjacent = Path(target) / ".workstate-bootstrap.json"
        observed.append(
            {
                "present": adjacent.is_file(),
                "source_kind": (
                    json.loads(adjacent.read_text()).get("source_kind")
                    if adjacent.is_file()
                    else None
                ),
            }
        )

    monkeypatch.setattr(install_module, "_run_init_state", spy_init)

    install_module.install(
        target=repo,
        source="worktree",
        mcp_servers={"workstate-handoff-mcp": {"command": "uv", "args": ["run"]}},
        enforce_required_surfaces=False,
    )
    assert observed, "init-state was never invoked"
    assert observed[0]["present"], (
        "adjacent manifest must exist before init-state (PD-02 ordering)"
    )
    assert observed[0]["source_kind"] == "worktree"


def test_apply_hooks_preserves_previously_managed_adapters(
    tmp_path: Path, fake_remote_with_generator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second apply-hooks call must MERGE, not replace: an adapter recorded
    by an earlier call must survive a later call that opts in a different
    flag (else a subsequent `update` silently de-manages it)."""
    install_module = _install_module()
    from workstate_bootstrap.subcommands import apply_hooks

    repo = _seed_worktree_repo(tmp_path, fake_remote_with_generator)
    _stub_heavy_install_steps(install_module, monkeypatch)
    install_module.install(
        target=repo,
        source="worktree",
        mcp_servers=None,
        enforce_required_surfaces=False,
    )

    def _hook_flags(manifest: dict) -> set[str]:
        return {
            entry.get("opt_in_flag")
            for entry in manifest.get("configs", [])
            if isinstance(entry, dict) and entry.get("kind") == "hook_adapter"
        }

    first = apply_hooks(target=repo, install_claude_error_hook=True)
    assert "--install-claude-error-hook" in _hook_flags(first)

    second = apply_hooks(target=repo, install_claude_stop_hook=True)
    assert "--install-claude-error-hook" in _hook_flags(second), (
        "apply-hooks must preserve a previously managed adapter on a later call"
    )


def _mk_intree_mcp_packages(repo: Path) -> None:
    for pkg in ("mcp-workstate-handoff", "mcp-workstate-orchestrator"):
        pkg_dir = repo / "packages" / pkg
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")


def test_build_local_default_mcp_servers_worktree_base(tmp_path: Path) -> None:
    from workstate_bootstrap.install import _build_local_default_mcp_servers

    repo = tmp_path / "wt"
    repo.mkdir()
    _mk_intree_mcp_packages(repo)

    servers = _build_local_default_mcp_servers(repo, base=repo)
    assert servers is not None
    handoff = servers["workstate-handoff-mcp"]
    assert handoff["command"] == "uv"
    args = handoff["args"]
    assert "--project" in args
    assert args[args.index("--project") + 1] == "packages/mcp-workstate-handoff"
    assert not any("uvx" in str(arg) for arg in args)
    # Default base (the managed clone) finds no in-tree packages -> None.
    assert _build_local_default_mcp_servers(repo) is None


def test_worktree_resolver_rewrites_default_to_local_launchers(
    tmp_path: Path,
) -> None:
    from workstate_bootstrap.install import (
        DEFAULT_MCP_SERVERS,
        _resolve_worktree_install_mcp_servers,
    )

    repo = tmp_path / "wt"
    repo.mkdir()
    _mk_intree_mcp_packages(repo)

    resolved = _resolve_worktree_install_mcp_servers(repo, DEFAULT_MCP_SERVERS)
    assert resolved is not DEFAULT_MCP_SERVERS
    assert resolved is not None
    assert resolved["workstate-handoff-mcp"]["command"] == "uv"

    # An explicit non-default map is honored verbatim.
    custom = {"x": {"command": "uvx", "args": ["foo"]}}
    assert _resolve_worktree_install_mcp_servers(repo, custom) is custom

    # No in-tree packages -> fall back to the published uvx map.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert (
        _resolve_worktree_install_mcp_servers(empty, DEFAULT_MCP_SERVERS)
        is DEFAULT_MCP_SERVERS
    )


def test_worktree_plan_enables_presync(tmp_path: Path) -> None:
    from workstate_bootstrap.install_plan import (
        InstallRequest,
        SourceResolver,
        build_install_plan,
    )

    repo = tmp_path / "wt"
    repo.mkdir()
    request = InstallRequest(
        target=repo,
        source="worktree",
        remote_url=None,
        remote_ref=None,
        package_root=None,
        mcp_servers={"workstate-handoff-mcp": {"command": "uv", "args": ["run"]}},
        plugin_overrides=None,
        reset_overrides=False,
        backup_overrides=False,
        enforce_required_surfaces=False,
        profile="all",
        install_claude_stop_hook=False,
        install_claude_stop_hook_local=False,
        install_codex_stop_hook=False,
        install_vscode_stop_hook=False,
        install_grok_stop_hook=False,
        install_claude_reinject_hook=False,
        install_claude_reinject_hook_local=False,
        install_claude_error_hook=False,
        install_claude_error_hook_local=False,
    )
    source = SourceResolver(
        root=repo,
        kind="worktree",
        base_anchor="a" * 40,
        surface_mode="symlink",
        remote_sha="a" * 40,
    )
    plan = build_install_plan(request, source, mcp_servers=request.mcp_servers)
    assert plan.run_presync_prewarm is True


def test_resolve_managed_servers_worktree_manifest_uses_worktree_base(
    tmp_path: Path,
) -> None:
    from workstate_bootstrap.cli import _resolve_managed_servers

    repo = tmp_path / "wt"
    repo.mkdir()
    _mk_intree_mcp_packages(repo)
    (repo / ".workstate-bootstrap.json").write_text(
        json.dumps(
            {
                "schema_version": 5,
                "source_kind": "worktree",
                "remote_sha": "a" * 40,
                "profile": "all",
                "surfaces": [],
                "configs": [],
                "mcp_servers": [],
            }
        )
        + "\n"
    )

    resolved = _resolve_managed_servers(repo, "default")
    assert resolved is not None
    assert resolved["workstate-handoff-mcp"]["command"] == "uv"
    args = resolved["workstate-handoff-mcp"]["args"]
    assert args[args.index("--project") + 1] == "packages/mcp-workstate-handoff"
