"""TDD gate for workstate-bootstrap minimal `install` flow.

Verifies the smallest end-to-end behavior that unblocks WORKSTATE-REF-17-10 implementation note:

    workstate_bootstrap.install.install(
        target=<consumer_root>,
        remote_url=<git url>,
        remote_ref=<tag or branch>,
    )

must

1. Clone the remote into ``<consumer_root>/.workstate/remote/`` at
   ``remote_ref`` (the directory must contain a ``.git`` folder).
2. Write ``<consumer_root>/.workstate-bootstrap.json`` carrying schema_version,
   remote_url, remote_ref, the resolved 40-char remote_sha, and an empty
   ``surfaces`` list (symlinks land in a follow-on slice).

Both the library entrypoint and the ``workstate-bootstrap install --target ...``
console script must satisfy the contract.

The test uses a local bare repository as the remote so it is offline-safe and
deterministic. No network is required.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def _handoff_src_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "mcp-workstate-handoff" / "src"


def _protocol_src_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "workstate-protocol" / "src"


def _handoff_pythonpath() -> str:
    return os.pathsep.join((str(_handoff_src_dir()), str(_protocol_src_dir())))


def _install_fake_uvx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    uvx = shim_dir / "uvx"
    uvx.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "mcp-workstate-handoff|mcp-workstate-handoff@*)\n"
        f"  shift\n  PYTHONPATH=\"{_handoff_pythonpath()}:$PYTHONPATH\" exec python -m workstate_handoff_mcp \"$@\"\n"
        "  ;;\n"
        "--from)\n"
        "  case \"$2\" in\n"
        "  mcp-workstate-handoff|mcp-workstate-handoff@*)\n"
        f"  shift 2\n  PYTHONPATH=\"{_handoff_pythonpath()}:$PYTHONPATH\" exec \"$@\"\n"
        "    ;;\n"
        "  esac\n"
        "  ;;\n"
        "esac\n"
        "echo \"unsupported uvx payload: $1\" >&2\n"
        "exit 1\n"
    )
    uvx.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ.get('PATH', '')}")


@pytest.fixture()
def fake_remote(tmp_path: Path) -> tuple[str, str]:
    """Build a local bare git remote with one tag ``v0.1.0``.

    Returns (remote_url, tag_name). The remote_url is a ``file://`` URL so it
    can be passed to ``git clone`` exactly as a real ``git+ssh://`` URL would
    be.
    """
    src = tmp_path / "remote-src"
    src.mkdir()
    _git("init", "--initial-branch=main", cwd=src)
    _git("config", "user.email", "test@example.com", cwd=src)
    _git("config", "user.name", "Test", cwd=src)
    (src / "skill.md").write_text("# fake skill\n")
    _git("add", "-A", cwd=src)
    _git("commit", "-m", "seed", cwd=src)
    _git("tag", "v0.1.0", cwd=src)

    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(src), str(bare), cwd=tmp_path)

    return f"file://{bare}", "v0.1.0"


# ---------------------------------------------------------------------------
# Library API contract
# ---------------------------------------------------------------------------


def test_install_clones_remote_into_dot_workstate(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    remote_url, ref = fake_remote

    install(target=target, remote_url=remote_url, remote_ref=ref)

    clone = target / ".workstate" / "remote"
    assert clone.is_dir(), "clone directory must exist"
    assert (clone / ".git").exists(), "clone must contain a .git directory"
    assert (clone / "skill.md").read_text() == "# fake skill\n"


def test_install_writes_overlay_manifest(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    remote_url, ref = fake_remote

    install(target=target, remote_url=remote_url, remote_ref=ref)

    manifest_path = target / ".workstate-bootstrap.json"
    assert manifest_path.is_file(), ".workstate-bootstrap.json must be written"

    manifest = json.loads(manifest_path.read_text())
    assert manifest["schema_version"] == 2
    assert manifest["remote_url"] == remote_url
    assert manifest["remote_ref"] == ref
    assert isinstance(manifest["remote_sha"], str)
    assert len(manifest["remote_sha"]) == 40, "remote_sha must be the full 40-char SHA"
    # WORKSTATE-REF-02 implementation note cutover: only the Copilot prompt surface remains a
    # generated per-agent directory. The cross-harness skill +
    # slash-command surfaces moved to the plugin tree (ADR-001) and are
    # no longer prepared or written by the generator.
    sources = {entry["path"]: entry["source"] for entry in manifest["surfaces"]}
    assert sources == {".github/prompts": "generated"}


def test_install_migrates_legacy_overlay_manifest(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    """A legacy ``.workstate-overlay.json`` written by an older bootstrap must be
    renamed to ``.workstate-bootstrap.json`` on first run, preserving its data."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    remote_url, ref = fake_remote

    legacy_payload = {
        "schema_version": 1,
        "remote_url": remote_url,
        "remote_ref": ref,
        "remote_sha": "a" * 40,
        "surfaces": [],
        "configs": [],
    }
    legacy_path = target / ".workstate-overlay.json"
    legacy_path.write_text(json.dumps(legacy_payload))

    install(target=target, remote_url=remote_url, remote_ref=ref)

    assert not legacy_path.exists(), "legacy .workstate-overlay.json must be removed"
    canonical = target / ".workstate-bootstrap.json"
    assert canonical.is_file()
    fresh = json.loads(canonical.read_text())
    # The migrated file is overwritten by the fresh install, but the rename
    # itself must have happened (verified above) and the file is now
    # canonical-named.
    assert fresh["schema_version"] == 2


def test_install_leaves_unrelated_overlay_file_untouched(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    """A consumer-owned ``.workstate-overlay.json`` that is NOT a bootstrap manifest
    (no list ``surfaces`` key) must not be renamed by the migration."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    remote_url, ref = fake_remote

    consumer_payload = {"my_overlay_config": {"theme": "dark"}}
    legacy_path = target / ".workstate-overlay.json"
    legacy_path.write_text(json.dumps(consumer_payload))

    install(target=target, remote_url=remote_url, remote_ref=ref)

    assert legacy_path.is_file(), "consumer-owned overlay file must not be migrated"
    assert json.loads(legacy_path.read_text()) == consumer_payload
    assert (target / ".workstate-bootstrap.json").is_file()


def test_install_is_idempotent_on_repeat_call(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    """Running ``install`` twice against the same target must not error and must
    leave the manifest pointing at the same remote_sha. Symlink reconciliation
    is a separate slice; this contract is just for the clone + manifest."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    remote_url, ref = fake_remote

    install(target=target, remote_url=remote_url, remote_ref=ref)
    first_sha = json.loads((target / ".workstate-bootstrap.json").read_text())["remote_sha"]

    install(target=target, remote_url=remote_url, remote_ref=ref)
    second_sha = json.loads((target / ".workstate-bootstrap.json").read_text())["remote_sha"]

    assert first_sha == second_sha


# ---------------------------------------------------------------------------
# Console script contract
# ---------------------------------------------------------------------------


def test_install_console_script_runs_end_to_end(
    tmp_path: Path, fake_remote: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``python -m workstate_bootstrap install --target <path> --remote-url ...
    --remote-ref ...`` must drive the same flow as the library entrypoint."""
    target = tmp_path / "consumer"
    target.mkdir()
    remote_url, ref = fake_remote

    _install_fake_uvx(monkeypatch, tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "workstate_bootstrap",
            "install",
            "--target",
            str(target),
            "--remote-url",
            remote_url,
            "--remote-ref",
            ref,
            # The minimal `fake_remote` fixture intentionally does not ship
            # scripts/hooks. The default install now enforces required
            # surfaces; opt out here since this test is about the console
            # script wiring, not the harness shape.
            "--no-enforce-required-surfaces",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"console script failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert (target / ".workstate" / "remote" / ".git").exists()
    assert (target / ".workstate-bootstrap.json").is_file()


# ---------------------------------------------------------------------------
# Refresh-path correctness (WORKSTATE-REF-17-10-BR13-H-01 + WORKSTATE-REF-17-10-BR13-M-01)
# ---------------------------------------------------------------------------


def _build_remote(workdir: Path, name: str, seed_file: str = "skill.md") -> tuple[Path, str]:
    """Build a fresh bare git remote at <workdir>/<name>.git seeded with one
    commit and tag ``v0.1.0``. Returns (bare_path, file_url)."""
    src = workdir / f"{name}-src"
    src.mkdir()
    _git("init", "--initial-branch=main", cwd=src)
    _git("config", "user.email", "test@example.com", cwd=src)
    _git("config", "user.name", "Test", cwd=src)
    (src / seed_file).write_text(f"# {name}\n")
    _git("add", "-A", cwd=src)
    _git("commit", "-m", f"seed {name}", cwd=src)
    _git("tag", "v0.1.0", cwd=src)
    bare = workdir / f"{name}.git"
    _git("clone", "--bare", str(src), str(bare), cwd=workdir)
    return bare, f"file://{bare}"


def test_install_rejects_existing_clone_with_different_remote_url(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    """WORKSTATE-REF-17-10-BR13-H-01: a rerun pointed at a different remote_url must not
    silently switch an unmanaged/inconsistent clone. Fail-fast with
    RemoteUrlMismatchError instead."""
    from workstate_bootstrap.install import RemoteUrlMismatchError, install

    target = tmp_path / "consumer"
    target.mkdir()
    first_url, ref = fake_remote
    install(target=target, remote_url=first_url, remote_ref=ref)
    manifest_path = target / ".workstate-bootstrap.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["remote_url"] = "git@example.com:unexpected/manifest.git"
    manifest_path.write_text(json.dumps(manifest))

    _, second_url = _build_remote(tmp_path, "other")

    with pytest.raises(RemoteUrlMismatchError) as exc:
        install(target=target, remote_url=second_url, remote_ref=ref)
    # Error must name both URLs so the operator can act on it.
    msg = str(exc.value)
    assert first_url in msg
    assert second_url in msg

    # Manifest must still describe the inconsistent remote; install did not
    # rewrite provenance or destroy the existing clone.
    manifest = json.loads(manifest_path.read_text())
    assert manifest["remote_url"] == "git@example.com:unexpected/manifest.git"


def test_install_switches_managed_clone_when_manifest_remote_changes(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    """A clone whose origin matches the previous bootstrap manifest is managed
    state, so changing remote_url is an intentional overlay switch. The clone is
    replaced and stale handoff state is archived before the new state is
    initialized."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    first_url, ref = fake_remote
    install(target=target, remote_url=first_url, remote_ref=ref)
    first_state = target / ".task-state" / "handoff.db"
    first_state.parent.mkdir()
    import sqlite3

    with sqlite3.connect(first_state):
        pass
    assert first_state.is_file()
    assert _git("remote", "get-url", "origin", cwd=target / ".workstate" / "remote") == first_url

    _, second_url = _build_remote(tmp_path, "other")
    result = install(
        target=target,
        remote_url=second_url,
        remote_ref=ref,
        mcp_servers=_runnable_handoff_server_spec(),
    )

    clone = target / ".workstate" / "remote"
    assert _git("remote", "get-url", "origin", cwd=clone) == second_url
    backup_root = target / result["state_backup_path"]
    assert (backup_root / ".task-state" / "handoff.db").is_file()
    assert (target / ".task-state" / "handoff.db").is_file()
    manifest = json.loads((target / ".workstate-bootstrap.json").read_text())
    assert manifest["remote_url"] == second_url


def test_install_branch_ref_advances_to_upstream_head(tmp_path: Path) -> None:
    """WORKSTATE-REF-17-10-BR13-M-01: rerunning install with a branch ref must move the
    clone to the latest upstream commit, not stay pinned to the original
    local branch ref after fetch."""
    from workstate_bootstrap.install import install

    # Build a remote with a 'main' branch carrying commit A.
    src = tmp_path / "branch-src"
    src.mkdir()
    _git("init", "--initial-branch=main", cwd=src)
    _git("config", "user.email", "test@example.com", cwd=src)
    _git("config", "user.name", "Test", cwd=src)
    (src / "a.md").write_text("a\n")
    _git("add", "-A", cwd=src)
    _git("commit", "-m", "A", cwd=src)
    bare = tmp_path / "branch.git"
    _git("clone", "--bare", str(src), str(bare), cwd=tmp_path)
    remote_url = f"file://{bare}"

    target = tmp_path / "consumer"
    target.mkdir()

    first = install(target=target, remote_url=remote_url, remote_ref="main")
    sha_a = first["remote_sha"]

    # Add commit B upstream and push it to the bare remote.
    (src / "b.md").write_text("b\n")
    _git("add", "-A", cwd=src)
    _git("commit", "-m", "B", cwd=src)
    _git("push", str(bare), "main", cwd=src)

    second = install(target=target, remote_url=remote_url, remote_ref="main")
    sha_b = second["remote_sha"]

    assert sha_b != sha_a, "branch ref must advance to fresh upstream commit"
    # Sanity: the new sha is the new upstream HEAD.
    new_head = _git("rev-parse", "HEAD", cwd=src)
    assert sha_b == new_head


# ---------------------------------------------------------------------------
# Symlink materialization for the six shared overlay surfaces
# ---------------------------------------------------------------------------


SHARED_SURFACES_EXPECTED = (
    ".github/hooks",
    "scripts/hooks",
    "docs/workstate/contracts",
    "docs/workstate/rules",
    "Makefile.d",
    "scripts/workstate",
)

# Standard git hook names shipped under ``scripts/hooks/git/`` in the real
# monorepo. The fake remote fixtures mirror this layout so the rehearsal
# can assert that ``core.hooksPath`` resolves to a directory containing
# git-resolvable hook files (regression for implementation note implementation note: a parent
# ``scripts/hooks/`` was previously set, which made git look up hook
# names at a path where only Python helpers lived).
SHARED_GIT_HOOK_NAMES = (
    "post-checkout",
    "post-commit",
    "post-merge",
    "post-rewrite",
    "pre-commit",
    "pre-push",
)
# implementation note implementation note: Python helpers shipped alongside the git hook
# scripts. ``check_branch_naming.py`` is the delegate that the
# pre-commit / pre-push / post-checkout hooks ``exec`` — if it is
# missing from the materialized ``scripts/hooks/`` surface, the
# implementation note/4/4b gates silently no-op.
SHARED_HOOK_HELPER_NAMES = ("check_branch_naming.py",)
GENERATED_SURFACES_EXPECTED = (".github/prompts",)


@pytest.fixture()
def fake_remote_with_surfaces(tmp_path: Path) -> tuple[str, str]:
    """Local bare git remote that ships the three shared overlay surfaces."""
    src = tmp_path / "rich-src"
    src.mkdir()
    _git("init", "--initial-branch=main", cwd=src)
    _git("config", "user.email", "test@example.com", cwd=src)
    _git("config", "user.name", "Test", cwd=src)
    for surface in SHARED_SURFACES_EXPECTED:
        d = src / surface
        d.mkdir(parents=True)
        (d / "MARKER.md").write_text(f"shared {surface}\n")
    # Mirror the real monorepo: ``scripts/hooks/git/<name>`` carries the
    # actual git hook scripts that ``core.hooksPath`` resolves against.
    git_hooks_dir = src / "scripts" / "hooks" / "git"
    git_hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in SHARED_GIT_HOOK_NAMES:
        hook = git_hooks_dir / name
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
    # implementation note implementation note: ship the helper script the hooks delegate to.
    for helper in SHARED_HOOK_HELPER_NAMES:
        helper_path = src / "scripts" / "hooks" / helper
        helper_path.write_text("#!/usr/bin/env python3\nimport sys; sys.exit(0)\n")
        helper_path.chmod(0o755)
    _git("add", "-A", cwd=src)
    _git("commit", "-m", "seed surfaces", cwd=src)
    _git("tag", "v0.1.0", cwd=src)
    bare = tmp_path / "rich.git"
    _git("clone", "--bare", str(src), str(bare), cwd=tmp_path)
    return f"file://{bare}", "v0.1.0"


def _workstate_system_root() -> Path | None:
    """Return packages/workstate-system inside this monorepo, or None when
    bootstrap is being tested in isolation (no sibling workstate-system)."""
    candidate = Path(__file__).resolve().parents[3] / "packages" / "workstate-system"
    return candidate if candidate.is_dir() else None


@pytest.fixture()
def fake_remote_with_generator(tmp_path: Path) -> tuple[str, str]:
    """Local bare git remote that mirrors the real monorepo layout: shared
    surfaces, generator, manifest, and canonical ``skills/`` source all live
    under ``packages/workstate-system/`` — same as
    ``workstate`` itself.

    Skipped when the sibling ``packages/workstate-system`` is not on disk
    (bootstrap-in-isolation test environments).
    """
    import shutil

    system_root = _workstate_system_root()
    if system_root is None:
        pytest.skip("packages/workstate-system not available in this environment")

    # implementation note S3: shipped overlay surfaces now live under the package's
    # ``workstate_system/payload/`` tree. Reads of the REAL package source
    # resolve payload-first, then fall back to the legacy top-level path for
    # surfaces that stayed (e.g. ``Makefile.d/evals.mk``, ``scripts/workstate/
    # evals``). The fake remote still mirrors the OLD consumer layout so the
    # bootstrap resolver's legacy fallback probe keeps resolving.
    payload_root = system_root / "workstate_system" / "payload"

    def _real_surface_dirs(rel: str) -> list[Path]:
        """Real source dirs for ``rel``, payload-first then legacy top-level.
        Partial-move surfaces (Makefile.d, scripts/workstate) exist in both."""
        return [d for d in (payload_root / rel, system_root / rel) if d.is_dir()]

    def _merge_copytree(sources: list[Path], target_dir: Path) -> None:
        for source_dir in sources:
            shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)

    src = tmp_path / "gen-src"
    src.mkdir()
    _git("init", "--initial-branch=main", cwd=src)
    _git("config", "user.email", "test@example.com", cwd=src)
    _git("config", "user.name", "Test", cwd=src)
    system_subdir = src / "packages" / "workstate-system"
    for surface in SHARED_SURFACES_EXPECTED:
        source_dirs = _real_surface_dirs(surface)
        target_dir = system_subdir / surface
        if source_dirs and any(any(d.iterdir()) for d in source_dirs):
            # Mirror the real surface so rehearsal tests see actual hoisted
            # content (e.g. implementation note implementation note needs Makefile.d/plans.mk's
            # launcher token to land verbatim).
            _merge_copytree(source_dirs, target_dir)
        else:
            target_dir.mkdir(parents=True)
            (target_dir / "MARKER.md").write_text(f"shared {surface}\n")
    # Mirror the real monorepo: ``scripts/hooks/git/<name>`` carries the
    # actual git hook scripts that ``core.hooksPath`` resolves against
    # (implementation note implementation note).
    git_hooks_dir = system_subdir / "scripts" / "hooks" / "git"
    git_hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in SHARED_GIT_HOOK_NAMES:
        hook = git_hooks_dir / name
        if hook.exists():
            continue
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
    # implementation note implementation note: ship the Python helpers the hooks delegate to
    # (e.g. check_branch_naming.py for the post-checkout/pre-commit/pre-push
    # branch-naming gates). Bootstrap symlinks the entire scripts/hooks/
    # surface, so the helpers come along automatically — but the install
    # rehearsal must pin their presence to catch a future regression.
    for helper in SHARED_HOOK_HELPER_NAMES:
        helper_path = system_subdir / "scripts" / "hooks" / helper
        if helper_path.exists():
            continue
        helper_path.write_text("#!/usr/bin/env python3\nimport sys; sys.exit(0)\n")
        helper_path.chmod(0o755)
    # Generator + manifest + neutral skill source. These surfaces MOVED to
    # the payload tree (implementation note S3); read them from payload_root but mirror
    # them into the fake remote at the OLD consumer layout.
    (system_subdir / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        payload_root / "scripts" / "generate_agent_workflows.py",
        system_subdir / "scripts" / "generate_agent_workflows.py",
    )
    shutil.copytree(
        payload_root / "config" / "agent-workflows",
        system_subdir / "config" / "agent-workflows",
    )
    shutil.copytree(payload_root / "skills", system_subdir / "skills")

    _git("add", "-A", cwd=src)
    _git("commit", "-m", "seed surfaces + generator", cwd=src)
    _git("tag", "v0.1.0", cwd=src)
    bare = tmp_path / "gen-remote.git"
    _git("clone", "--bare", str(src), str(bare), cwd=tmp_path)
    return f"file://{bare}", "v0.1.0"


def test_install_runs_generator_and_populates_per_agent_surfaces(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """End-to-end: when the overlay clone ships the generator, ``install``
    runs it and the Copilot prompt surface is populated with real
    artifacts (not just empty directories).

    WORKSTATE-REF-02 implementation note cutover: the cross-harness ``.claude/skills``,
    ``.claude/commands``, and ``.codex/skills`` surfaces have moved to
    the plugin tree (ADR-001) and are no longer emitted by the
    generator; only ``.github/prompts`` is asserted populated here."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator

    install(target=target, remote_url=url, remote_ref=ref)

    prompts = target / ".github" / "prompts"

    assert any(prompts.glob("*.prompt.md")), "Copilot prompt files must be generated"

    # Every generated surface stays a real directory, not a symlink.
    for surface in GENERATED_SURFACES_EXPECTED:
        assert (target / surface).is_dir()
        assert not (target / surface).is_symlink()


def test_install_skips_cross_harness_skill_emission_for_portable_slugs(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """WORKSTATE-REF-02 implementation note cutover regression: after ``install`` no portable
    slug from ``portable_commands.json`` is emitted under
    ``.claude/commands/<id>.md``, ``.claude/skills/<slug>/SKILL.md``, or
    ``.codex/skills/<slug>/SKILL.md``. The plugin tree owns these
    surfaces now; bootstrap delegates to the same generator and must
    inherit the trim."""
    import json

    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator

    install(target=target, remote_url=url, remote_ref=ref)

    manifest_src = (
        target
        / ".workstate"
        / "remote"
        / "config"
        / "agent-workflows"
        / "portable_commands.json"
    )
    if not manifest_src.exists():
        # Fall back to the monorepo path layout used by the fake remote.
        manifest_src = (
            target
            / ".workstate"
            / "remote"
            / "packages"
            / "workstate-system"
            / "config"
            / "agent-workflows"
            / "portable_commands.json"
        )
    payload = json.loads(manifest_src.read_text())
    portable_slugs = {cmd["skill"] for cmd in payload["commands"]}
    assert portable_slugs, "fake remote must seed portable command manifest"

    for slug in portable_slugs:
        assert not (target / ".claude" / "commands" / f"{slug}.md").exists(), (
            f".claude/commands/{slug}.md must not be emitted after WORKSTATE-REF-02 cutover"
        )
        assert not (target / ".claude" / "skills" / slug).exists(), (
            f".claude/skills/{slug}/ must not be emitted after WORKSTATE-REF-02 cutover"
        )
        assert not (target / ".codex" / "skills" / slug).exists(), (
            f".codex/skills/{slug}/ must not be emitted after WORKSTATE-REF-02 cutover"
        )

    # The Copilot prompt surface is still the per-slug carrier.
    for slug in portable_slugs:
        assert (target / ".github" / "prompts" / f"{slug}.prompt.md").exists(), (
            f"Copilot prompt for {slug} must still be emitted"
        )


def test_install_discovers_plugin_override_root_and_composes_effective_tree(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    override_root = target / "workstate-overrides" / "workstate-system"
    override_skill_dir = override_root / "skills" / "branch-review"
    override_skill_dir.mkdir(parents=True)
    (override_skill_dir / "SKILL.md").write_text(
        "---\nname: branch-review\ndescription: local override\n---\n\nBootstrap-composed override body.\n"
    )

    # implementation note S3: skills/ moved into the package payload tree.
    system_root = (
        Path(__file__).resolve().parents[2]
        / "workstate-system"
        / "workstate_system"
        / "payload"
    )
    structured = yaml.safe_load((system_root / "skills" / "branch-review" / "skill.yaml").read_text())
    structured.pop("generator", None)
    body = (system_root / "skills" / "branch-review" / "body.md").read_text()
    fm_text = yaml.safe_dump(structured, sort_keys=False, default_flow_style=False).rstrip()
    base_skill = f"---\n{fm_text}\n---\n\n{body if body.endswith(chr(10)) else body + chr(10)}"
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

    url, ref = fake_remote_with_generator
    manifest = install(target=target, remote_url=url, remote_ref=ref)

    base_skill_path = (
        target
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "base"
        / "claude"
        / "skills"
        / "branch-review"
        / "SKILL.md"
    )
    effective_skill_path = (
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
    assert base_skill_path.is_file(), "install must materialize the base plugin tree"
    assert effective_skill_path.is_file(), "install must materialize the effective plugin tree"
    assert "Bootstrap-composed override body." not in base_skill_path.read_text()
    assert "Bootstrap-composed override body." in effective_skill_path.read_text()

    plugin_lock = (
        target
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "effective"
        / "plugin-lock.json"
    )
    assert plugin_lock.is_file(), "effective tree install must emit plugin-lock.json"
    assert manifest["remote_sha"] == json.loads(plugin_lock.read_text())["base_remote_sha"]


def test_install_writes_tracked_override_lock_for_plugin_overrides(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install
    from workstate_protocol.bootstrap import PluginOverrideLock

    target = tmp_path / "consumer"
    target.mkdir()
    override_root = target / "workstate-overrides" / "workstate-system"
    override_skill_dir = override_root / "skills" / "branch-review"
    override_skill_dir.mkdir(parents=True)
    (override_skill_dir / "SKILL.md").write_text(
        "---\nname: branch-review\ndescription: local override\n---\n\nBootstrap-composed override body.\n"
    )

    # implementation note S3: skills/ moved into the package payload tree.
    system_root = (
        Path(__file__).resolve().parents[2]
        / "workstate-system"
        / "workstate_system"
        / "payload"
    )
    structured = yaml.safe_load((system_root / "skills" / "branch-review" / "skill.yaml").read_text())
    structured.pop("generator", None)
    body = (system_root / "skills" / "branch-review" / "body.md").read_text()
    fm_text = yaml.safe_dump(structured, sort_keys=False, default_flow_style=False).rstrip()
    base_skill = f"---\n{fm_text}\n---\n\n{body if body.endswith(chr(10)) else body + chr(10)}"
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

    url, ref = fake_remote_with_generator
    manifest = install(target=target, remote_url=url, remote_ref=ref)

    override_lock = override_root / "overrides.lock.json"
    assert override_lock.is_file(), "install must emit overrides.lock.json for tracked override provenance"

    payload = json.loads(override_lock.read_text())
    assert payload["base_remote_sha"] == manifest["remote_sha"]
    assert any(
        entry["name"] == "branch-review"
        and entry["mode"] == "replace"
        and entry["local_path"] == "skills/branch-review/SKILL.md"
        and entry["upstream_digest"] == f"sha256:{upstream_digest}"
        for entry in payload["components"]
    )
    PluginOverrideLock.model_validate(payload)


def test_install_accepts_explicit_plugin_override_root(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    override_root = target / "custom-overrides" / "workstate-system"
    override_skill_dir = override_root / "skills" / "branch-review"
    override_skill_dir.mkdir(parents=True)
    (override_skill_dir / "SKILL.md").write_text(
        "---\nname: branch-review\ndescription: local override\n---\n\nBootstrap-composed override body.\n"
    )

    # implementation note S3: skills/ moved into the package payload tree.
    system_root = (
        Path(__file__).resolve().parents[2]
        / "workstate-system"
        / "workstate_system"
        / "payload"
    )
    structured = yaml.safe_load((system_root / "skills" / "branch-review" / "skill.yaml").read_text())
    structured.pop("generator", None)
    body = (system_root / "skills" / "branch-review" / "body.md").read_text()
    fm_text = yaml.safe_dump(structured, sort_keys=False, default_flow_style=False).rstrip()
    base_skill = f"---\n{fm_text}\n---\n\n{body if body.endswith(chr(10)) else body + chr(10)}"
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

    url, ref = fake_remote_with_generator
    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        plugin_overrides=override_root,
    )

    effective_skill_path = (
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
    assert effective_skill_path.is_file()
    assert "Bootstrap-composed override body." in effective_skill_path.read_text()
    assert manifest["plugin_overrides_path"] == "custom-overrides/workstate-system"


def test_install_console_script_accepts_plugin_overrides_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
) -> None:
    _install_fake_uvx(monkeypatch, tmp_path)

    target = tmp_path / "consumer"
    target.mkdir()
    override_root = target / "custom-overrides" / "workstate-system"
    override_skill_dir = override_root / "skills" / "branch-review"
    override_skill_dir.mkdir(parents=True)
    (override_skill_dir / "SKILL.md").write_text(
        "---\nname: branch-review\ndescription: local override\n---\n\nBootstrap-composed override body.\n"
    )

    # implementation note S3: skills/ moved into the package payload tree.
    system_root = (
        Path(__file__).resolve().parents[2]
        / "workstate-system"
        / "workstate_system"
        / "payload"
    )
    structured = yaml.safe_load((system_root / "skills" / "branch-review" / "skill.yaml").read_text())
    structured.pop("generator", None)
    body = (system_root / "skills" / "branch-review" / "body.md").read_text()
    fm_text = yaml.safe_dump(structured, sort_keys=False, default_flow_style=False).rstrip()
    base_skill = f"---\n{fm_text}\n---\n\n{body if body.endswith(chr(10)) else body + chr(10)}"
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

    url, ref = fake_remote_with_generator
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
            "--plugin-overrides",
            str(override_root),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (
        target
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "effective"
        / "plugin-lock.json"
    ).is_file()


def test_install_points_plugin_pins_at_effective_tree_when_overrides_exist(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install
    import tomllib

    target = tmp_path / "consumer"
    target.mkdir()
    override_root = target / "workstate-overrides" / "workstate-system"
    override_skill_dir = override_root / "skills" / "branch-review"
    override_skill_dir.mkdir(parents=True)
    (override_skill_dir / "SKILL.md").write_text(
        "---\nname: branch-review\ndescription: local override\n---\n\nBootstrap-composed override body.\n"
    )

    # implementation note S3: skills/ moved into the package payload tree.
    system_root = (
        Path(__file__).resolve().parents[2]
        / "workstate-system"
        / "workstate_system"
        / "payload"
    )
    structured = yaml.safe_load((system_root / "skills" / "branch-review" / "skill.yaml").read_text())
    structured.pop("generator", None)
    body = (system_root / "skills" / "branch-review" / "body.md").read_text()
    fm_text = yaml.safe_dump(structured, sort_keys=False, default_flow_style=False).rstrip()
    base_skill = f"---\n{fm_text}\n---\n\n{body if body.endswith(chr(10)) else body + chr(10)}"
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

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    claude_marketplace = json.loads((target / ".claude-plugin" / "marketplace.json").read_text())
    assert claude_marketplace["plugins"][0]["source"] == (
        "./.workstate/generated/plugins/workstate-system/effective/claude"
    )

    claude_settings = json.loads((target / ".claude" / "settings.json").read_text())
    assert claude_settings["extraKnownMarketplaces"]["workstate-marketplace"]["source"] == {
        "source": "directory",
        "path": ".",
    }
    assert claude_settings["enabledPlugins"]["workstate-system@workstate-marketplace"] is True

    codex_marketplace = json.loads((target / ".agents" / "plugins" / "marketplace.json").read_text())
    assert codex_marketplace["plugins"][0]["source"] == {
        "source": "local",
        "path": "./.workstate/generated/plugins/workstate-system/effective/codex",
    }

    codex_config = tomllib.loads((target / ".codex" / "config.toml").read_text())
    assert codex_config["marketplaces"]["workstate-marketplace"] == {
        "source_type": "local",
        "source": ".",
    }
    assert codex_config["plugins"]["workstate-system@workstate-marketplace"][
        "enabled"
    ] is True


def test_install_preserves_explicit_plugin_disable_when_overrides_exist(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install
    import tomllib

    target = tmp_path / "consumer"
    target.mkdir()
    override_root = target / "workstate-overrides" / "workstate-system"
    override_skill_dir = override_root / "skills" / "branch-review"
    override_skill_dir.mkdir(parents=True)
    (override_skill_dir / "SKILL.md").write_text(
        "---\nname: branch-review\ndescription: local override\n---\n\nBootstrap-composed override body.\n"
    )

    # implementation note S3: skills/ moved into the package payload tree.
    system_root = (
        Path(__file__).resolve().parents[2]
        / "workstate-system"
        / "workstate_system"
        / "payload"
    )
    structured = yaml.safe_load((system_root / "skills" / "branch-review" / "skill.yaml").read_text())
    structured.pop("generator", None)
    body = (system_root / "skills" / "branch-review" / "body.md").read_text()
    fm_text = yaml.safe_dump(structured, sort_keys=False, default_flow_style=False).rstrip()
    base_skill = f"---\n{fm_text}\n---\n\n{body if body.endswith(chr(10)) else body + chr(10)}"
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
    settings_path = target / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {
                "extraKnownMarketplaces": {
                    "workstate-marketplace": {
                        "source": {"source": "directory", "path": "."}
                    }
                },
                "enabledPlugins": {
                    "workstate-system@workstate-marketplace": False,
                },
            }
        )
    )
    codex_config_path = target / ".codex" / "config.toml"
    codex_config_path.parent.mkdir(parents=True, exist_ok=True)
    codex_config_path.write_text(
        '[plugins."workstate-system@workstate-marketplace"]\n'
        "enabled = false\n"
    )

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    settings = json.loads(settings_path.read_text())
    assert settings["enabledPlugins"]["workstate-system@workstate-marketplace"] is False
    codex_config = tomllib.loads(codex_config_path.read_text())
    assert codex_config["plugins"]["workstate-system@workstate-marketplace"][
        "enabled"
    ] is False


def test_install_reverts_effective_tree_pins_to_base_tree_when_overrides_disappear(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    override_root = target / "workstate-overrides" / "workstate-system"
    override_skill_dir = override_root / "skills" / "branch-review"
    override_skill_dir.mkdir(parents=True)
    (override_skill_dir / "SKILL.md").write_text(
        "---\nname: branch-review\ndescription: local override\n---\n\nBootstrap-composed override body.\n"
    )

    # implementation note S3: skills/ moved into the package payload tree.
    system_root = (
        Path(__file__).resolve().parents[2]
        / "workstate-system"
        / "workstate_system"
        / "payload"
    )
    structured = yaml.safe_load((system_root / "skills" / "branch-review" / "skill.yaml").read_text())
    structured.pop("generator", None)
    body = (system_root / "skills" / "branch-review" / "body.md").read_text()
    fm_text = yaml.safe_dump(structured, sort_keys=False, default_flow_style=False).rstrip()
    base_skill = f"---\n{fm_text}\n---\n\n{body if body.endswith(chr(10)) else body + chr(10)}"
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

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    shutil.rmtree(override_root)
    shutil.rmtree(target / ".workstate" / "generated" / "plugins" / "workstate-system" / "effective")

    install(target=target, remote_url=url, remote_ref=ref)

    claude_marketplace = json.loads((target / ".claude-plugin" / "marketplace.json").read_text())
    assert claude_marketplace["plugins"][0]["source"] == (
        "./.workstate/generated/plugins/workstate-system/base/claude"
    )

    codex_marketplace = json.loads((target / ".agents" / "plugins" / "marketplace.json").read_text())
    assert codex_marketplace["plugins"][0]["source"] == {
        "source": "local",
        "path": "./.workstate/generated/plugins/workstate-system/base/codex",
    }


def test_install_reset_overrides_removes_clean_override_root(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _git("init", "--initial-branch=main", cwd=target)
    _git("config", "user.email", "test@example.com", cwd=target)
    _git("config", "user.name", "Test", cwd=target)

    override_root = target / "workstate-overrides" / "workstate-system"
    override_skill_dir = override_root / "skills" / "branch-review"
    override_skill_dir.mkdir(parents=True)
    (override_skill_dir / "SKILL.md").write_text(
        "---\nname: branch-review\ndescription: local override\n---\n\nBootstrap-composed override body.\n"
    )

    # implementation note S3: skills/ moved into the package payload tree.
    system_root = (
        Path(__file__).resolve().parents[2]
        / "workstate-system"
        / "workstate_system"
        / "payload"
    )
    structured = yaml.safe_load((system_root / "skills" / "branch-review" / "skill.yaml").read_text())
    structured.pop("generator", None)
    body = (system_root / "skills" / "branch-review" / "body.md").read_text()
    fm_text = yaml.safe_dump(structured, sort_keys=False, default_flow_style=False).rstrip()
    base_skill = f"---\n{fm_text}\n---\n\n{body if body.endswith(chr(10)) else body + chr(10)}"
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

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)
    _git("add", "-A", cwd=target)
    _git("commit", "-m", "baseline override install", cwd=target)

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        reset_overrides=True,
    )

    assert not override_root.exists()
    assert manifest.get("plugin_overrides_path") is None

    claude_marketplace = json.loads((target / ".claude-plugin" / "marketplace.json").read_text())
    assert claude_marketplace["plugins"][0]["source"] == (
        "./.workstate/generated/plugins/workstate-system/base/claude"
    )


def test_install_reset_overrides_refuses_dirty_worktree_by_default(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import OverrideResetRequiresBackupError, install

    target = tmp_path / "consumer"
    target.mkdir()
    _git("init", "--initial-branch=main", cwd=target)
    _git("config", "user.email", "test@example.com", cwd=target)
    _git("config", "user.name", "Test", cwd=target)

    override_root = target / "workstate-overrides" / "workstate-system"
    override_skill_dir = override_root / "skills" / "branch-review"
    override_skill_dir.mkdir(parents=True)
    (override_skill_dir / "SKILL.md").write_text(
        "---\nname: branch-review\ndescription: local override\n---\n\nBootstrap-composed override body.\n"
    )
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {},
            },
            sort_keys=False,
        )
    )

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref, enforce_required_surfaces=False)

    with pytest.raises(OverrideResetRequiresBackupError):
        install(
            target=target,
            remote_url=url,
            remote_ref=ref,
            reset_overrides=True,
            enforce_required_surfaces=False,
        )


def test_install_reset_overrides_with_backup_archives_dirty_override_root(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _git("init", "--initial-branch=main", cwd=target)
    _git("config", "user.email", "test@example.com", cwd=target)
    _git("config", "user.name", "Test", cwd=target)

    override_root = target / "workstate-overrides" / "workstate-system"
    override_skill_dir = override_root / "skills" / "branch-review"
    override_skill_dir.mkdir(parents=True)
    (override_skill_dir / "SKILL.md").write_text(
        "---\nname: branch-review\ndescription: local override\n---\n\nBootstrap-composed override body.\n"
    )
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {},
            },
            sort_keys=False,
        )
    )

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref, enforce_required_surfaces=False)

    result = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        reset_overrides=True,
        backup_overrides=True,
        enforce_required_surfaces=False,
    )

    backup_path = result.get("override_backup_path")
    assert isinstance(backup_path, str) and backup_path
    archived_root = target / backup_path / "workstate-system"
    assert not override_root.exists()
    assert archived_root.is_dir()
    assert (archived_root / "skills" / "branch-review" / "SKILL.md").is_file()


def test_install_reset_overrides_does_not_prune_external_override_parents(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _git("init", "--initial-branch=main", cwd=target)
    _git("config", "user.email", "test@example.com", cwd=target)
    _git("config", "user.name", "Test", cwd=target)

    external_parent = tmp_path / "external-overrides"
    override_root = external_parent / "workstate-system"
    override_skill_dir = override_root / "skills" / "branch-review"
    override_skill_dir.mkdir(parents=True)
    (override_skill_dir / "SKILL.md").write_text(
        "---\nname: branch-review\ndescription: local override\n---\n\nBootstrap-composed override body.\n"
    )
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {},
            },
            sort_keys=False,
        )
    )

    url, ref = fake_remote_with_generator
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        plugin_overrides=override_root,
        enforce_required_surfaces=False,
    )
    _git("add", "-A", cwd=target)
    _git("commit", "-m", "baseline external override install", cwd=target)

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        plugin_overrides=override_root,
        reset_overrides=True,
        enforce_required_surfaces=False,
    )

    assert not override_root.exists()
    assert external_parent.is_dir()


def test_shared_surface_set_includes_docs_workflow_rules() -> None:
    """WORKSTATE-REF-50 implementation note: ``docs/workstate/rules`` must be a hoisted shared
    surface so the canonical rule docs (``branch-review-guide.md``,
    ``development-workflow.md``, ``planning-artifact-home.md``) reach
    consumers via the bootstrap regenerate path. Prior to this slice
    only ``docs/workstate/contracts`` was hoisted, leaving rule docs
    repo-local."""
    from workstate_bootstrap.install import SHARED_SURFACES

    assert "docs/workstate/rules" in SHARED_SURFACES, (
        "docs/workstate/rules must be in SHARED_SURFACES so canonical rule "
        "docs (planning-artifact-home.md, development-workflow.md, "
        "branch-review-guide.md) propagate to consumers"
    )


def test_planning_artifact_home_rule_materializes(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """WORKSTATE-REF-50 implementation note: after a clean install, the consumer worktree
    must expose ``docs/workstate/rules/planning-artifact-home.md`` (and
    the two pre-existing rule docs) under the materialized symlink.
    Uses ``fake_remote_with_generator`` because that fixture copies the
    real ``packages/workstate-system/docs/workstate/rules/`` content — the
    bare-MARKER fixture would only ship a placeholder file."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator

    install(target=target, remote_url=url, remote_ref=ref)

    rules_dir = target / "docs" / "workstate" / "rules"
    assert rules_dir.is_symlink(), "rules directory must be hoisted as a symlink"
    expected_rule_docs = (
        "planning-artifact-home.md",
        "development-workflow.md",
        "branch-review-guide.md",
    )
    for name in expected_rule_docs:
        path = rules_dir / name
        assert path.is_file(), f"{name} must materialize in the consumer worktree"

    home_rule = (rules_dir / "planning-artifact-home.md").read_text()
    assert "# Planning Artifact Home" in home_rule
    assert "## Canonical homes" in home_rule
    assert "## Recovery" in home_rule


def test_install_materializes_known_shared_surfaces(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """The SHARED_SURFACES present in the remote become symlinks
    under <target>/<surface> pointing into <target>/.workstate/remote/<surface>,
    and each appears in manifest['surfaces'] with source='shared'.

    Per-agent surfaces (.claude/skills, .claude/commands, .github/prompts,
    .codex/skills) are *generated* into the target as real directories,
    not symlinks; covered by test_install_prepares_generated_surfaces.
    """
    from workstate_bootstrap.install import (
        GENERATED_SURFACES,
        SHARED_SURFACES,
        install,
    )

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_surfaces

    manifest = install(target=target, remote_url=url, remote_ref=ref)

    from workstate_bootstrap.install import SURFACE_CHILD_EXCLUSIONS

    assert tuple(SHARED_SURFACES) == SHARED_SURFACES_EXPECTED, (
        "SHARED_SURFACES must match the documented shared-surface tuple"
    )
    assert tuple(GENERATED_SURFACES) == GENERATED_SURFACES_EXPECTED

    by_path = {entry["path"]: entry for entry in manifest["surfaces"]}
    for surface in SHARED_SURFACES:
        if surface in SURFACE_CHILD_EXCLUSIONS:
            # Carved surfaces materialize as a real directory whose
            # non-excluded children are individual symlinks. The fixture
            # ships only MARKER.md, which has no excluded/lifecycle name, so
            # it becomes a per-child symlink under the real parent dir.
            parent = target / surface
            assert parent.is_dir() and not parent.is_symlink(), (
                f"{surface} must be a real directory (carved), not a symlink"
            )
            child = parent / "MARKER.md"
            assert child.is_symlink(), f"{surface}/MARKER.md must be a symlink"
            assert child.resolve() == (
                target / ".workstate" / "remote" / surface / "MARKER.md"
            ).resolve()
            assert child.read_text() == f"shared {surface}\n"
            assert by_path[f"{surface}/MARKER.md"]["source"] == "shared"
            assert surface not in by_path, (
                f"carved {surface} must not have a bare parent manifest entry"
            )
        else:
            link = target / surface
            assert link.is_symlink(), f"{surface} must be a symlink"
            assert link.resolve() == (target / ".workstate" / "remote" / surface).resolve()
            assert (link / "MARKER.md").read_text() == f"shared {surface}\n"
            assert by_path[surface]["source"] == "shared", by_path[surface]


def test_materialize_surfaces_carves_excluded_children_out(tmp_path: Path) -> None:
    """WS-REBRAND-01 Phase A: SHARED_SURFACES listed in
    SURFACE_CHILD_EXCLUSIONS materialize as a real directory whose excluded
    children (the evals harness) are fully absent from the consumer tree.

    Every non-excluded child becomes an individual symlink into the clone.
    The lifecycle children are left for ``_install_lifecycle_profile`` to
    hoist, so this pass neither symlinks them nor records a manifest entry
    for them (avoids a duplicate ``source='lifecycle'`` collision).
    """
    from workstate_bootstrap.install import _materialize_surfaces

    clone = tmp_path / "clone"
    base = clone / "packages" / "workstate-system"

    sa = base / "scripts" / "workstate"
    (sa / "evals").mkdir(parents=True)
    (sa / "evals" / "registry.py").write_text("# evals\n")
    (sa / "lifecycle").mkdir(parents=True)
    (sa / "lifecycle" / "__init__.py").write_text("")
    (sa / "git-plan-cat.sh").write_text("#!/bin/sh\n")

    md = base / "Makefile.d"
    md.mkdir(parents=True)
    (md / "evals.mk").write_text("# evals targets\n")
    (md / "lifecycle.mk").write_text("# lifecycle targets\n")
    (md / "plans.mk").write_text("# plan targets\n")

    sh = base / "scripts" / "hooks"
    sh.mkdir(parents=True)
    (sh / "MARKER.md").write_text("hook\n")

    target = tmp_path / "consumer"
    target.mkdir()

    entries = _materialize_surfaces(target, clone)
    by_path = {e["path"]: e for e in entries}

    # Carved parents are real directories, not whole-directory symlinks.
    assert (target / "scripts" / "workstate").is_dir()
    assert not (target / "scripts" / "workstate").is_symlink()
    assert (target / "Makefile.d").is_dir()
    assert not (target / "Makefile.d").is_symlink()

    # The evals harness is fully absent and unrecorded.
    assert not (target / "scripts" / "workstate" / "evals").exists()
    assert "scripts/workstate/evals" not in by_path
    assert not (target / "Makefile.d" / "evals.mk").exists()
    assert "Makefile.d/evals.mk" not in by_path

    # Lifecycle children are deferred to the lifecycle hoist: no symlink and
    # no manifest entry from the carve pass.
    assert not (target / "scripts" / "workstate" / "lifecycle").exists()
    assert "scripts/workstate/lifecycle" not in by_path
    assert not (target / "Makefile.d" / "lifecycle.mk").exists()
    assert "Makefile.d/lifecycle.mk" not in by_path

    # Every other child becomes a per-child symlink, recorded source='shared'.
    plan_cat = target / "scripts" / "workstate" / "git-plan-cat.sh"
    assert plan_cat.is_symlink()
    assert plan_cat.resolve() == (sa / "git-plan-cat.sh").resolve()
    assert by_path["scripts/workstate/git-plan-cat.sh"]["source"] == "shared"
    plans_mk = target / "Makefile.d" / "plans.mk"
    assert plans_mk.is_symlink()
    assert by_path["Makefile.d/plans.mk"]["source"] == "shared"

    # No bare parent surface entry for carved surfaces.
    assert "scripts/workstate" not in by_path
    assert "Makefile.d" not in by_path

    # Non-carved surfaces keep the whole-directory symlink behavior.
    assert (target / "scripts" / "hooks").is_symlink()
    assert by_path["scripts/hooks"]["source"] == "shared"


def test_materialize_surfaces_removes_bootstrap_owned_excluded_children(
    tmp_path: Path,
) -> None:
    """Upgrade path: a real carved parent may already contain stale child
    symlinks into the bootstrap remote. Excluded children owned by that remote
    must be removed so retired eval surfaces disappear on rerun.
    """
    from workstate_bootstrap.install import _materialize_surfaces

    clone = tmp_path / "clone"
    base = clone / "packages" / "workstate-system"

    sa = base / "scripts" / "workstate"
    (sa / "evals").mkdir(parents=True)
    (sa / "evals" / "registry.py").write_text("# evals\n")
    (sa / "git-plan-cat.sh").write_text("#!/bin/sh\n")

    md = base / "Makefile.d"
    md.mkdir(parents=True)
    (md / "evals.mk").write_text("# evals targets\n")
    (md / "plans.mk").write_text("# plan targets\n")

    target = tmp_path / "consumer"
    target.mkdir()

    scripts_workstate = target / "scripts" / "workstate"
    scripts_workstate.mkdir(parents=True)
    (scripts_workstate / "evals").symlink_to(
        os.path.relpath(sa / "evals", scripts_workstate),
        target_is_directory=True,
    )
    makefile_d = target / "Makefile.d"
    makefile_d.mkdir()
    (makefile_d / "evals.mk").symlink_to(
        os.path.relpath(md / "evals.mk", makefile_d)
    )

    entries = _materialize_surfaces(target, clone)
    by_path = {e["path"]: e for e in entries}

    assert not (target / "scripts" / "workstate" / "evals").exists()
    assert not (target / "Makefile.d" / "evals.mk").exists()
    assert "scripts/workstate/evals" not in by_path
    assert "Makefile.d/evals.mk" not in by_path
    assert (target / "scripts" / "workstate" / "git-plan-cat.sh").is_symlink()
    assert by_path["scripts/workstate/git-plan-cat.sh"]["source"] == "shared"
    assert (target / "Makefile.d" / "plans.mk").is_symlink()
    assert by_path["Makefile.d/plans.mk"]["source"] == "shared"


def test_install_prepares_generated_surfaces_as_real_directories(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """Per-agent surfaces are real directories ready for the generator,
    not symlinks into the overlay clone."""
    from workstate_bootstrap.install import GENERATED_SURFACES, install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_surfaces

    manifest = install(target=target, remote_url=url, remote_ref=ref)

    by_path = {entry["path"]: entry for entry in manifest["surfaces"]}
    for surface in GENERATED_SURFACES:
        path = target / surface
        assert path.exists(), f"{surface} must exist after install"
        assert path.is_dir(), f"{surface} must be a real directory"
        assert not path.is_symlink(), (
            f"{surface} must be a real directory, not a symlink "
            "(generator writes into it)"
        )
        assert by_path[surface]["source"] == "generated"


def test_install_symlinks_are_idempotent(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """Re-running install must not duplicate, replace, or break existing
    symlinks the bootstrap itself created. Uses a SHARED surface
    (scripts/hooks) since per-agent surfaces are now generated, not
    symlinked."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_surfaces

    install(target=target, remote_url=url, remote_ref=ref)
    hooks_link = target / "scripts" / "hooks"
    first_target = os.readlink(hooks_link)

    install(target=target, remote_url=url, remote_ref=ref)

    assert hooks_link.is_symlink()
    assert os.readlink(hooks_link) == first_target


def test_install_preserves_existing_local_surface(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """If a SHARED surface path already exists as a real local directory,
    install must leave it untouched and record source='local' in the
    manifest. Per-agent surfaces are no longer 'shared' — they are
    always 'generated' — so this test now uses scripts/hooks as the
    local-override candidate."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    local_hooks = target / "scripts" / "hooks"
    local_hooks.mkdir(parents=True)
    (local_hooks / "local-hook.sh").write_text("#!/bin/sh\necho local\n")

    url, ref = fake_remote_with_surfaces
    manifest = install(target=target, remote_url=url, remote_ref=ref)

    assert local_hooks.is_dir() and not local_hooks.is_symlink()
    assert (local_hooks / "local-hook.sh").read_text().endswith("echo local\n")

    by_path = {entry["path"]: entry for entry in manifest["surfaces"]}
    assert by_path["scripts/hooks"]["source"] == "local"
    assert by_path[".github/hooks"]["source"] == "shared"


def test_install_skips_shared_surfaces_missing_in_remote(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    """A remote that does not ship any SHARED surface produces no shared
    symlinks. Generated surfaces are still prepared (empty dirs) so the
    generator has somewhere to write — but the lean fixture has no
    generator script either, so the dirs stay empty."""
    from workstate_bootstrap.install import GENERATED_SURFACES, install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote  # lean fixture: only ships skill.md, no surface dirs

    manifest = install(target=target, remote_url=url, remote_ref=ref)

    by_path = {entry["path"]: entry for entry in manifest["surfaces"]}
    # No shared surfaces materialized.
    shared_entries = [e for e in manifest["surfaces"] if e["source"] == "shared"]
    assert shared_entries == []
    assert not (target / "scripts" / "hooks").exists()

    # Generated surfaces are still prepared as real (empty) directories.
    for surface in GENERATED_SURFACES:
        assert (target / surface).is_dir()
        assert by_path[surface]["source"] == "generated"


# ---------------------------------------------------------------------------
# WORKSTATE-REF-57: stale shared-surface symlink repoint
# ---------------------------------------------------------------------------
#
# When a consumer was installed before the overlay layout moved into
# ``packages/workstate-system/``, the target-side symlink can still point at
# the legacy clone-root path (e.g. ``scripts/hooks -> ../.workstate/remote/
# scripts/hooks``). Before WORKSTATE-REF-57, ``_materialize_surfaces`` accepted that
# symlink as ``source='shared'`` because the resolved path lexically sat under
# ``clone_resolved`` — even when the resolved path no longer existed.
# These six tests pin the three-bucket classification from
# packages/workstate-bootstrap/docs/tasks/WORKSTATE-REF-57-stale-symlink-repoint-task-plan.md:
#
#   (1) live + resolves to expected source -> idempotent, source='shared'
#   (2) broken, OR live but resolves elsewhere, with raw link target
#       lexically under <target>/.workstate/remote/ -> repoint, source='shared',
#       emit `repointed: <surface>` to stdout
#   (3) live or broken with raw link target outside <target>/.workstate/remote/
#       -> foreign local content, source='local', no log line


def test_install_repoints_stale_shared_surface_symlink(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """A stale ``scripts/hooks`` symlink left over from the pre-v0.2.0 layout
    (target string ``../.workstate/remote/scripts/hooks``) must be repointed to
    the current nested source (``../.workstate/remote/packages/workstate-system/
    scripts/hooks``) and an audit line emitted to stdout."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    (target / "scripts").mkdir()
    stale_link = target / "scripts" / "hooks"
    # Lexically under <target>/.workstate/remote/ but at the legacy root path
    # that does not exist in the nested-layout remote.
    os.symlink("../.workstate/remote/scripts/hooks", stale_link)
    assert stale_link.is_symlink()
    assert not stale_link.exists(), "precondition: stale symlink target is broken"

    url, ref = fake_remote_with_generator
    manifest = install(target=target, remote_url=url, remote_ref=ref)

    expected = target / ".workstate" / "remote" / "packages" / "workstate-system" / "scripts" / "hooks"
    assert stale_link.is_symlink()
    assert stale_link.resolve() == expected.resolve(), (
        f"stale symlink should resolve to nested source, got {os.readlink(stale_link)}"
    )

    captured = capsys.readouterr()
    assert "repointed: scripts/hooks" in captured.out, (
        f"expected `repointed: scripts/hooks` on stdout; got stdout={captured.out!r} stderr={captured.err!r}"
    )

    by_path = {entry["path"]: entry for entry in manifest["surfaces"]}
    assert by_path["scripts/hooks"]["source"] == "shared"


def test_install_idempotent_rerun_does_not_rewrite_shared_surface_symlink(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """When a target-side symlink already resolves to the current expected
    source, the second install must NOT rewrite it and must NOT emit a
    `repointed:` audit line."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_surfaces

    install(target=target, remote_url=url, remote_ref=ref)
    hooks_link = target / "scripts" / "hooks"
    first_readlink = os.readlink(hooks_link)

    capsys.readouterr()  # drop output from the first install
    install(target=target, remote_url=url, remote_ref=ref)
    captured = capsys.readouterr()

    assert hooks_link.is_symlink()
    assert os.readlink(hooks_link) == first_readlink, (
        "idempotent rerun rewrote the symlink — link target changed"
    )
    assert "repointed:" not in captured.out, (
        f"idempotent rerun should not emit `repointed:`; got {captured.out!r}"
    )


def test_install_preserves_foreign_shared_surface_symlink_as_local(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """A symlink whose raw target points OUTSIDE <target>/.workstate/remote/
    is operator-owned foreign content. It must be left untouched and the
    manifest must classify it as ``source='local'``; no audit line is
    emitted."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    (target / "scripts").mkdir()
    foreign_target = tmp_path / "operator-overlay" / "hooks"
    foreign_target.mkdir(parents=True)
    (foreign_target / "MARKER.md").write_text("operator-owned\n")
    foreign_link = target / "scripts" / "hooks"
    os.symlink(foreign_target, foreign_link)

    url, ref = fake_remote_with_surfaces
    manifest = install(target=target, remote_url=url, remote_ref=ref)
    captured = capsys.readouterr()

    assert foreign_link.is_symlink()
    assert foreign_link.resolve() == foreign_target.resolve(), (
        "foreign symlink target must not be rewritten"
    )
    assert (foreign_link / "MARKER.md").read_text() == "operator-owned\n"

    by_path = {entry["path"]: entry for entry in manifest["surfaces"]}
    assert by_path["scripts/hooks"]["source"] == "local"
    assert "repointed: scripts/hooks" not in captured.out


def test_install_preserves_broken_shared_surface_symlink_outside_remote_as_local(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken symlink whose raw target is OUTSIDE <target>/.workstate/remote/
    is foreign local content (the operator pointed it somewhere we do not
    own). The install must not repoint it and must classify it as
    ``source='local'``."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    (target / "scripts").mkdir()
    broken_link = target / "scripts" / "hooks"
    os.symlink("/opt/nonexistent/hooks", broken_link)
    assert broken_link.is_symlink()
    assert not broken_link.exists()

    url, ref = fake_remote_with_surfaces
    manifest = install(target=target, remote_url=url, remote_ref=ref)
    captured = capsys.readouterr()

    assert broken_link.is_symlink()
    assert os.readlink(broken_link) == "/opt/nonexistent/hooks", (
        "broken-outside-remote symlink target must not be rewritten"
    )

    by_path = {entry["path"]: entry for entry in manifest["surfaces"]}
    assert by_path["scripts/hooks"]["source"] == "local"
    assert "repointed: scripts/hooks" not in captured.out


def test_install_manifest_classifies_repaired_stale_symlink_as_shared(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """After a stale symlink is repointed, the manifest must record the
    surface as ``source='shared'`` (not ``'local'``), so downstream
    consumers (sync, doctor) treat it as bootstrap-managed."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    (target / "scripts").mkdir()
    os.symlink("../.workstate/remote/scripts/hooks", target / "scripts" / "hooks")

    url, ref = fake_remote_with_generator
    manifest = install(target=target, remote_url=url, remote_ref=ref)

    by_path = {entry["path"]: entry for entry in manifest["surfaces"]}
    assert by_path["scripts/hooks"]["source"] == "shared", by_path["scripts/hooks"]


def test_install_repointed_stale_symlink_helper_resolves_end_to_end(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    """End-to-end: after a stale ``scripts/hooks`` symlink is repointed,
    the hoisted helper ``scripts/hooks/check_branch_naming.py`` must be
    reachable through the repaired symlink. This is the consumer-facing
    failure mode WORKSTATE-REF-57 was reported under."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    (target / "scripts").mkdir()
    os.symlink("../.workstate/remote/scripts/hooks", target / "scripts" / "hooks")

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    helper = target / "scripts" / "hooks" / "check_branch_naming.py"
    assert helper.exists(), (
        f"helper must resolve through the repointed symlink; "
        f"readlink={os.readlink(target / 'scripts' / 'hooks')}"
    )


# ---------------------------------------------------------------------------
# Config writers (.mcp.json, .vscode/mcp.json, .codex/config.toml, hooksPath)
# ---------------------------------------------------------------------------


SAMPLE_MCP_SERVERS = {
    "workstate-handoff-mcp": {
        "command": sys.executable,
        "args": ["-m", "workstate_handoff_mcp"],
        "env": {"PYTHONPATH": _handoff_pythonpath()},
    },
}


def _runnable_handoff_server_spec() -> dict[str, dict[str, object]]:
    return {
        "workstate-handoff-mcp": {
            "command": sys.executable,
            "args": ["-m", "workstate_handoff_mcp"],
            "env": {"PYTHONPATH": _handoff_pythonpath()},
        }
    }


def _load_local_install_module(monkeypatch: pytest.MonkeyPatch):
    import importlib

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "src"))
    sys.modules.pop("workstate_bootstrap.install", None)
    sys.modules.pop("workstate_bootstrap", None)
    return importlib.import_module("workstate_bootstrap.install")


def _init_git_repo(path: Path) -> None:
    _git("init", "--initial-branch=main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def test_install_writes_all_four_config_surfaces_when_mcp_servers_provided(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    """When ``mcp_servers`` is provided and target is a git repo, install must
    write ``.mcp.json``, ``.vscode/mcp.json``, ``.codex/config.toml`` and set
    ``core.hooksPath`` — and record each surface in ``manifest['configs']``."""
    from workstate_bootstrap.install import install
    import tomllib

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    mcp_doc = json.loads((target / ".mcp.json").read_text())
    assert mcp_doc["mcpServers"]["workstate-handoff-mcp"]["command"] == sys.executable
    assert mcp_doc["mcpServers"]["workstate-handoff-mcp"]["args"] == [
        "-m",
        "workstate_handoff_mcp",
    ]

    vscode_doc = json.loads((target / ".vscode" / "mcp.json").read_text())
    assert vscode_doc["servers"]["workstate-handoff-mcp"]["command"] == sys.executable

    codex_doc = tomllib.loads((target / ".codex" / "config.toml").read_text())
    assert codex_doc["mcp_servers"]["workstate-handoff-mcp"]["command"] == sys.executable
    assert codex_doc["mcp_servers"]["workstate-handoff-mcp"]["args"] == [
        "-m",
        "workstate_handoff_mcp",
    ]

    hooks_path = _git("config", "--get", "core.hooksPath", cwd=target)
    assert hooks_path == "scripts/hooks/git"

    by_path = {entry["path"]: entry for entry in manifest["configs"]}
    # WORKSTATE-REF-56 implementation note: the harness Stop-hook is opt-in via the
    # manifest walker; the default install no longer writes
    # ``.claude/settings.local.json`` automatically.
    # Plugin pin files are managed only when the overlay ships the plugin
    # generator inputs. This fixture intentionally uses a minimal legacy
    # remote, so install must not write marketplace pins to missing trees.
    # WORKSTATE-REF-48: ``--profile all`` (the default) hoists the lifecycle
    # runner, so the sentinel-bracketed ``-include`` directive lands a
    # ``Makefile`` config entry alongside the MCP-server surfaces.
    assert set(by_path) == {
        ".mcp.json",
        ".vscode/mcp.json",
        ".codex/config.toml",
        "core.hooksPath",
        "Makefile",
        # implementation note S4: managed overlay-ignore block keeps git status clean.
        ".gitignore",
    }
    assert by_path[".mcp.json"]["action"] == "created"
    assert by_path["core.hooksPath"]["action"] == "set"


def test_install_deep_merges_existing_mcp_json(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    """A pre-existing ``.mcp.json`` with unrelated servers and top-level keys
    must keep all of them; only the bootstrap-managed server entries get
    overwritten."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    pre_existing = {
        "mcpServers": {
            "user-tool": {"command": "node", "args": ["server.js"]},
            "workstate-handoff-mcp": {"command": "OLD", "args": ["stale"]},
        },
        "userKey": {"keepMe": True},
    }
    (target / ".mcp.json").write_text(json.dumps(pre_existing, indent=2))

    url, ref = fake_remote
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    doc = json.loads((target / ".mcp.json").read_text())
    # User's other server preserved.
    assert doc["mcpServers"]["user-tool"] == {"command": "node", "args": ["server.js"]}
    # User's top-level key preserved.
    assert doc["userKey"] == {"keepMe": True}
    # Managed server overwritten with new values.
    assert doc["mcpServers"]["workstate-handoff-mcp"]["command"] == sys.executable
    assert doc["mcpServers"]["workstate-handoff-mcp"]["args"] == [
        "-m",
        "workstate_handoff_mcp",
    ]


def test_install_codex_config_preserves_user_tables_and_comments(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    """tomlkit-backed write must preserve unrelated ``[other]`` tables, root
    keys, and trailing user comments. Only the managed
    ``[mcp_servers.<name>]`` table gets replaced."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    codex_dir = target / ".codex"
    codex_dir.mkdir()

    pre_existing = (
        "# user comment at top\n"
        'model = "gpt-5"\n'
        "\n"
        "[other]\n"
        'value = "keep"\n'
        "\n"
        "[mcp_servers.workstate-handoff-mcp]\n"
        'command = "STALE"\n'
        'args = ["old"]\n'
    )
    (codex_dir / "config.toml").write_text(pre_existing)

    url, ref = fake_remote
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    text = (codex_dir / "config.toml").read_text()
    assert "# user comment at top" in text, "user comment must survive"
    assert 'model = "gpt-5"' in text, "user root key must survive"
    assert "[other]" in text and 'value = "keep"' in text, "user table must survive"

    import tomllib

    doc = tomllib.loads(text)
    assert doc["mcp_servers"]["workstate-handoff-mcp"]["command"] == sys.executable
    assert doc["mcp_servers"]["workstate-handoff-mcp"]["args"] == [
        "-m",
        "workstate_handoff_mcp",
    ]
    assert doc["other"]["value"] == "keep"


def test_install_skips_hooks_path_when_target_not_git_repo(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    """When target is not a git repo, hooksPath is silently skipped and
    ``manifest['configs']`` does not include a ``core.hooksPath`` entry."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()  # no git init
    url, ref = fake_remote

    manifest = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    paths = {entry["path"] for entry in manifest["configs"]}
    assert ".mcp.json" in paths, "JSON writers still run"
    assert ".codex/config.toml" in paths
    assert "core.hooksPath" not in paths


def test_install_with_default_sentinel_uses_built_in_server_map(
    tmp_path: Path, fake_remote: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``install(mcp_servers="default")`` resolves to ``DEFAULT_MCP_SERVERS``
    and writes the three managed-config files (implementation note step 2a)."""
    import json as _json

    from workstate_bootstrap.install import DEFAULT_MCP_SERVERS, install

    _install_fake_uvx(monkeypatch, tmp_path)

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote

    manifest = install(target=target, remote_url=url, remote_ref=ref, mcp_servers="default")

    mcp_doc = _json.loads((target / ".mcp.json").read_text())
    vscode_doc = _json.loads((target / ".vscode" / "mcp.json").read_text())
    assert set(mcp_doc["mcpServers"].keys()) == set(DEFAULT_MCP_SERVERS.keys())
    assert set(vscode_doc["servers"].keys()) == set(DEFAULT_MCP_SERVERS.keys())
    for entry in mcp_doc["mcpServers"].values():
        assert entry["type"] == "stdio"
        assert "--workspace-root" in entry["args"]
        assert entry["args"][-1] == "serve-stdio"
    assert mcp_doc["mcpServers"]["workstate-handoff-mcp"]["args"][0] == "mcp-workstate-handoff@0.12.1"
    assert mcp_doc["mcpServers"]["workstate-orchestrator-mcp"]["args"][0] == "mcp-workstate-orchestrator@0.5.2"
    for entry in vscode_doc["servers"].values():
        assert entry["type"] == "stdio"
        assert "--workspace-root" in entry["args"]
        assert entry["args"][-1] == "serve-stdio"
    assert (target / ".codex" / "config.toml").is_file()
    paths = {entry["path"] for entry in manifest["configs"]}
    assert {".mcp.json", ".vscode/mcp.json", ".codex/config.toml"}.issubset(paths)


def test_install_rejects_unknown_mcp_servers_sentinel(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote

    with pytest.raises(ValueError, match="not a recognized sentinel"):
        install(target=target, remote_url=url, remote_ref=ref, mcp_servers="bogus")


def test_install_cli_default_writes_managed_servers(
    tmp_path: Path, fake_remote: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling the CLI without ``--mcp-servers`` writes all three
    managed-config files thanks to the default-on resolver. After
    WORKSTATE-REF-56 implementation note, ``all`` is once again the CLI default profile
    (matching the library ``install()`` API), so a no-argument invocation
    exercises this path."""
    import json as _json

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote

    _install_fake_uvx(monkeypatch, tmp_path)

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
            "--no-enforce-required-surfaces",
            "--profile",
            "all",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    mcp_doc = _json.loads((target / ".mcp.json").read_text())
    assert "workstate-handoff-mcp" in mcp_doc["mcpServers"]
    assert "workstate-orchestrator-mcp" in mcp_doc["mcpServers"]


def test_install_cli_no_mcp_servers_opt_out(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote

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
            "--no-mcp-servers",
            "--no-enforce-required-surfaces",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not (target / ".mcp.json").exists()
    assert not (target / ".vscode" / "mcp.json").exists()
    assert not (target / ".codex" / "config.toml").exists()


def test_install_without_mcp_servers_writes_no_config_files(
    tmp_path: Path, fake_remote: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``mcp_servers`` is None, the three MCP-server file-writers
    are skipped. ``core.hooksPath`` still runs because it does not
    depend on the server map. WORKSTATE-REF-56 implementation note: the harness Stop hook
    is opt-in via the manifest walker and is NOT written by default.
    """
    from workstate_bootstrap.install import install

    # Sandbox HOME defensively so any future manifest adapter that
    # targets a user-scoped path cannot escape the tmp dir during this
    # test run.
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake-home"))
    (tmp_path / "fake-home").mkdir()

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote

    manifest = install(target=target, remote_url=url, remote_ref=ref)

    assert not (target / ".mcp.json").exists()
    assert not (target / ".vscode" / "mcp.json").exists()
    assert not (target / ".codex" / "config.toml").exists()

    paths = {entry["path"] for entry in manifest["configs"]}
    # WORKSTATE-REF-56 implementation note: the harness Stop hook is opt-in via the manifest
    # walker; without ``--install-claude-stop-hook[-local]`` it does not
    # appear in the config manifest.
    # Plugin pin files are managed only when the overlay ships the plugin
    # generator inputs. This fixture intentionally uses a minimal legacy
    # remote, so install must not write marketplace pins to missing trees.
    # WORKSTATE-REF-48: ``--profile all`` (the default) hoists the lifecycle
    # runner, so the sentinel-bracketed include lands the Makefile
    # config entry alongside hooksPath.
    assert paths == {
        "core.hooksPath",
        "Makefile",
        # implementation note S4: managed overlay-ignore block (git repo target).
        ".gitignore",
    }


def test_install_with_runnable_handoff_server_bootstraps_state_db(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=_runnable_handoff_server_spec(),
    )

    assert (target / ".task-state" / "handoff.db").is_file()
    assert (target / ".task-state" / "exports").is_dir()


def test_run_init_state_retries_with_cloned_handoff_project_when_uvx_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_module = _load_local_install_module(monkeypatch)

    target = tmp_path / "consumer"
    target.mkdir()
    project = target / ".workstate" / "remote" / "packages" / "mcp-workstate-handoff"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        "[project]\nname = \"mcp-workstate-handoff\"\nversion = \"0.0.0\"\n"
    )

    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        rendered = list(cmd)
        calls.append((rendered, dict(kwargs)))
        if rendered[0] == "uvx":
            raise subprocess.CalledProcessError(
                1,
                rendered,
                output="",
                stderr="No solution found when resolving tool dependencies",
            )
        return subprocess.CompletedProcess(rendered, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(install_module.subprocess, "run", fake_run)

    install_module._run_init_state(
        target,
        install_module.DEFAULT_MCP_SERVERS,
        expected_remote_url="git@example.com:private/workstate.git",
    )

    assert [cmd[0] for cmd, _ in calls] == ["uvx", "uv"]
    assert calls[0][0][1] == "mcp-workstate-handoff@0.12.1"
    assert calls[1][0][:5] == [
        "uv",
        "run",
        "--project",
        str(project),
        "mcp-workstate-handoff",
    ]
    assert calls[1][0][-2:] == [
        "--expected-remote-url",
        "git@example.com:private/workstate.git",
    ]
    assert calls[0][1]["cwd"] == str(target)
    assert calls[1][1]["cwd"] == str(target)


def test_branch_install_uses_cloned_local_mcp_servers_for_default_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_module = _load_local_install_module(monkeypatch)

    target = tmp_path / "consumer"
    target.mkdir()
    handoff_project = target / ".workstate" / "remote" / "packages" / "mcp-workstate-handoff"
    orchestrator_project = target / ".workstate" / "remote" / "packages" / "mcp-workstate-orchestrator"
    handoff_project.mkdir(parents=True)
    orchestrator_project.mkdir(parents=True)
    (handoff_project / "pyproject.toml").write_text(
        "[project]\nname = \"mcp-workstate-handoff\"\nversion = \"0.0.0\"\n"
    )
    (orchestrator_project / "pyproject.toml").write_text(
        "[project]\nname = \"mcp-workstate-orchestrator\"\nversion = \"0.0.0\"\n"
    )

    resolved = install_module._resolve_install_mcp_servers(
        target,
        "main",
        install_module.DEFAULT_MCP_SERVERS,
    )

    assert resolved is not install_module.DEFAULT_MCP_SERVERS
    assert resolved == {
        "workstate-handoff-mcp": {
            "type": "stdio",
            "command": "uv",
            "args": [
                "run",
                "--no-sync",
                "--project",
                ".workstate/remote/packages/mcp-workstate-handoff",
                "mcp-workstate-handoff",
                "--workspace-root",
                ".",
                "serve-stdio",
            ],
        },
        "workstate-orchestrator-mcp": {
            "type": "stdio",
            "command": "uv",
            "args": [
                "run",
                "--no-sync",
                "--project",
                ".workstate/remote/packages/mcp-workstate-orchestrator",
                "mcp-workstate-orchestrator",
                "--workspace-root",
                ".",
                "serve-stdio",
            ],
        },
    }


def _build_remote_with_local_mcp_projects(tmp_path: Path) -> tuple[str, str]:
    src = tmp_path / "local-mcp-src"
    src.mkdir()
    _git("init", "--initial-branch=main", cwd=src)
    _git("config", "user.email", "test@example.com", cwd=src)
    _git("config", "user.name", "Test", cwd=src)
    handoff_project = src / "packages" / "mcp-workstate-handoff"
    orchestrator_project = src / "packages" / "mcp-workstate-orchestrator"
    handoff_project.mkdir(parents=True)
    orchestrator_project.mkdir(parents=True)
    (handoff_project / "pyproject.toml").write_text(
        "[project]\nname = \"mcp-workstate-handoff\"\nversion = \"0.0.0\"\n"
    )
    (orchestrator_project / "pyproject.toml").write_text(
        "[project]\nname = \"mcp-workstate-orchestrator\"\nversion = \"0.0.0\"\n"
    )
    _git("add", "-A", cwd=src)
    _git("commit", "-m", "seed local mcp projects", cwd=src)
    bare = tmp_path / "local-mcp.git"
    _git("clone", "--bare", str(src), str(bare), cwd=tmp_path)
    return f"file://{bare}", "main"


def test_install_presyncs_branch_default_local_mcp_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_module = _load_local_install_module(monkeypatch)

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = _build_remote_with_local_mcp_projects(tmp_path)

    calls: list[dict[str, object]] = []

    def fake_presync(
        target_arg: Path,
        mcp_servers: dict[str, dict[str, object]],
    ) -> list[Path]:
        calls.append({"target": target_arg, "mcp_servers": mcp_servers})
        return []

    monkeypatch.setattr(install_module, "_presync_local_mcp_envs", fake_presync)
    monkeypatch.setattr(install_module, "_prepare_state_for_remote_switch", lambda target_arg, remote_url: (remote_url, None))
    monkeypatch.setattr(install_module, "_materialize_surfaces", lambda target_arg, clone: [])
    monkeypatch.setattr(install_module, "_prepare_generated_surfaces", lambda target_arg, clone: [])
    monkeypatch.setattr(install_module, "_prepare_plugin_generated_surfaces", lambda target_arg, clone, override_root: [])
    monkeypatch.setattr(install_module, "_run_generator", lambda *args, **kwargs: None)
    monkeypatch.setattr(install_module, "_write_configs", lambda target_arg, mcp_servers, include_hooks=True: [])
    monkeypatch.setattr(install_module, "_run_init_state", lambda *args, **kwargs: None)

    install_module.install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers="default",
        enforce_required_surfaces=False,
    )

    assert len(calls) == 1
    mcp_servers = calls[0]["mcp_servers"]
    assert isinstance(mcp_servers, dict)
    handoff_args = mcp_servers["workstate-handoff-mcp"]["args"]
    orchestrator_args = mcp_servers["workstate-orchestrator-mcp"]["args"]
    assert handoff_args[:3] == ["run", "--no-sync", "--project"]
    assert orchestrator_args[:3] == ["run", "--no-sync", "--project"]


def test_install_lean_profiles_do_not_presync_local_mcp_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_module = _load_local_install_module(monkeypatch)

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = _build_remote_with_local_mcp_projects(tmp_path)

    calls: list[object] = []

    def fake_presync(target_arg: Path, mcp_servers: dict[str, dict[str, object]]) -> list[Path]:
        calls.append((target_arg, mcp_servers))
        return []

    monkeypatch.setattr(install_module, "_presync_local_mcp_envs", fake_presync)

    install_module.install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers="default",
        profile=install_module.PROFILE_MINIMAL,
        enforce_required_surfaces=False,
    )

    assert calls == []


def test_preserved_local_mcp_specs_gain_no_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_module = _load_local_install_module(monkeypatch)

    target = tmp_path / "consumer"
    target.mkdir()
    project = target / ".workstate" / "remote" / "packages" / "mcp-workstate-handoff"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        "[project]\nname = \"mcp-workstate-handoff\"\nversion = \"0.0.0\"\n"
    )
    preserved = {
        "workstate-handoff-mcp": {
            "type": "stdio",
            "command": "uv",
            "args": [
                "run",
                "--project",
                ".workstate/remote/packages/mcp-workstate-handoff",
                "mcp-workstate-handoff",
                "serve-stdio",
            ],
        }
    }

    # implementation note A1 implementation note: the resolver no longer normalizes; the render
    # seam owns the --no-sync invariant. The resolver passes preserved specs
    # through unchanged...
    resolved = install_module._resolve_install_mcp_servers(target, "main", preserved)
    assert resolved == preserved
    assert resolved["workstate-handoff-mcp"]["args"][1] == "--project"

    # ...and the write seam injects --no-sync for the preserved local launcher.
    rendered = json.loads(install_module._render_mcp_json(target, resolved).decode())
    assert rendered["mcpServers"]["workstate-handoff-mcp"]["args"][:3] == [
        "run",
        "--no-sync",
        "--project",
    ]


def test_presync_local_mcp_envs_syncs_each_local_uv_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_module = _load_local_install_module(monkeypatch)

    target = tmp_path / "consumer"
    target.mkdir()
    handoff_project = target / ".workstate" / "remote" / "packages" / "mcp-workstate-handoff"
    orchestrator_project = target / ".workstate" / "remote" / "packages" / "mcp-workstate-orchestrator"
    handoff_project.mkdir(parents=True)
    orchestrator_project.mkdir(parents=True)
    (handoff_project / "pyproject.toml").write_text(
        "[project]\nname = \"mcp-workstate-handoff\"\nversion = \"0.0.0\"\n"
    )
    (orchestrator_project / "pyproject.toml").write_text(
        "[project]\nname = \"mcp-workstate-orchestrator\"\nversion = \"0.0.0\"\n"
    )

    resolved = install_module._resolve_install_mcp_servers(
        target,
        "main",
        install_module.DEFAULT_MCP_SERVERS,
    )

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, stdout="", stderr="")

    monkeypatch.setattr(install_module.subprocess, "run", fake_run)

    synced = install_module._presync_local_mcp_envs(target, resolved)

    assert synced == [handoff_project.resolve(), orchestrator_project.resolve()]
    assert calls == [
        ["uv", "sync", "--project", str(handoff_project.resolve())],
        ["uv", "sync", "--project", str(orchestrator_project.resolve())],
    ]


def test_presync_local_mcp_envs_skips_uvx_package_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_module = _load_local_install_module(monkeypatch)

    target = tmp_path / "consumer"
    target.mkdir()

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, stdout="", stderr="")

    monkeypatch.setattr(install_module.subprocess, "run", fake_run)

    # DEFAULT_MCP_SERVERS uses uvx (package profile); nothing to pre-sync.
    synced = install_module._presync_local_mcp_envs(
        target, install_module.DEFAULT_MCP_SERVERS
    )

    assert synced == []
    assert calls == []


def test_run_init_state_surfaces_uvx_error_when_no_cloned_handoff_project_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_module = _load_local_install_module(monkeypatch)

    target = tmp_path / "consumer"
    target.mkdir()

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        rendered = list(cmd)
        calls.append(rendered)
        raise subprocess.CalledProcessError(
            1,
            rendered,
            output="",
            stderr="No solution found when resolving tool dependencies",
        )

    monkeypatch.setattr(install_module.subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError) as exc:
        install_module._run_init_state(target, install_module.DEFAULT_MCP_SERVERS)

    assert calls == [[
        "uvx",
        "mcp-workstate-handoff@0.12.1",
        "--workspace-root",
        ".",
        "--state-dir",
        str(target / ".task-state"),
        "init-state",
    ]]
    assert exc.value.cmd[0] == "uvx"


def test_install_does_not_create_task_state_when_required_surface_missing(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    """PLAN0003-S3-BR-001: required-surface refusal must run BEFORE init-state
    so a failing install does not leave .task-state/ behind on disk.

    The minimal ``fake_remote`` fixture intentionally ships no ``scripts/hooks``
    surface, so an enforcing install must raise before any state-init runs.
    """
    from workstate_bootstrap.install import (
        BootstrapManifestValidationError,
        install,
    )

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote

    with pytest.raises(BootstrapManifestValidationError):
        install(
            target=target,
            remote_url=url,
            remote_ref=ref,
            mcp_servers=_runnable_handoff_server_spec(),
            enforce_required_surfaces=True,
        )

    # No state files left behind from the failed install.
    assert not (target / ".task-state").exists()
    # No manifest written either.
    assert not (target / ".workstate-bootstrap.json").exists()


def test_install_archives_state_and_reinitializes_when_remote_url_changes(
    tmp_path: Path, fake_remote: tuple[str, str]
) -> None:
    """PLAN0003-S3-BR-002: install must not reuse state from a different
    remote_url. Instead it archives the old runtime state, then initializes a
    fresh DB against the new remote manifest."""
    import sqlite3

    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote

    # Plant a stale .task-state/handoff.db plus an .workstate-bootstrap.json that
    # points at a *different* remote_url. The remote_url guard inside
    # init-state must refuse to reuse that DB rather than silently adopting it.
    state_dir = target / ".task-state"
    state_dir.mkdir()
    with sqlite3.connect(state_dir / "handoff.db"):
        pass
    (target / ".workstate-bootstrap.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "remote_url": "git@example.com:some/other-overlay.git",
                "remote_ref": "main",
                "remote_sha": "a" * 40,
                "surfaces": [],
                "configs": [],
            }
        )
    )

    result = install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=_runnable_handoff_server_spec(),
    )

    backup_root = target / result["state_backup_path"]
    assert backup_root.is_dir()
    assert (backup_root / ".task-state" / "handoff.db").is_file()
    assert (target / ".task-state" / "handoff.db").is_file()

    manifest = json.loads((target / ".workstate-bootstrap.json").read_text())
    assert manifest["remote_url"] == url


def test_render_mcp_json_enforces_no_sync_on_local_uv_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """implementation note A1: the render seam injects --no-sync into local
    `uv run --project` launchers regardless of how the spec was resolved,
    so every entry command (install/update/repair/mcp-sync) writes the
    same contention-free launcher."""
    install_module = _load_local_install_module(monkeypatch)

    target = tmp_path / "consumer"
    target.mkdir()
    servers = {
        "workstate-handoff-mcp": {
            "type": "stdio",
            "command": "uv",
            "args": [
                "run",
                "--project",
                ".workstate/remote/packages/mcp-workstate-handoff",
                "mcp-workstate-handoff",
                "--workspace-root",
                ".",
                "serve-stdio",
            ],
        }
    }

    rendered = json.loads(install_module._render_mcp_json(target, servers).decode())
    args = rendered["mcpServers"]["workstate-handoff-mcp"]["args"]
    assert args[:3] == ["run", "--no-sync", "--project"], args
    assert args.count("--no-sync") == 1, args


def test_render_mcp_json_leaves_nonlocal_uv_project_specs_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """implementation note A1 still only applies to managed local launchers under the
    target worktree; arbitrary external ``uv run --project`` specs must not
    be rewritten at the managed render seam."""
    install_module = _load_local_install_module(monkeypatch)

    target = tmp_path / "consumer"
    target.mkdir()
    external_project = tmp_path / "external-server"
    external_project.mkdir()
    (external_project / "pyproject.toml").write_text(
        "[project]\nname = \"external-server\"\nversion = \"0.0.0\"\n"
    )
    servers = {
        "external-mcp": {
            "type": "stdio",
            "command": "uv",
            "args": [
                "run",
                "--project",
                str(external_project),
                "external-mcp",
                "serve-stdio",
            ],
        }
    }

    rendered = json.loads(install_module._render_mcp_json(target, servers).decode())
    args = rendered["mcpServers"]["external-mcp"]["args"]
    assert args[:2] == ["run", "--project"], args
    assert "--no-sync" not in args, args


def test_render_vscode_and_codex_enforce_no_sync_on_local_uv_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """implementation note A1 implementation note: the remaining two managed surfaces
    (.vscode/mcp.json, .codex/config.toml) funnel through the same
    render seam, so a local `uv run --project` launcher gains --no-sync
    there too — every managed surface is now consistent."""
    import tomllib

    install_module = _load_local_install_module(monkeypatch)

    target = tmp_path / "consumer"
    target.mkdir()
    servers = {
        "workstate-handoff-mcp": {
            "command": "uv",
            "args": [
                "run",
                "--project",
                ".workstate/remote/packages/mcp-workstate-handoff",
                "mcp-workstate-handoff",
                "--workspace-root",
                ".",
                "serve-stdio",
            ],
        }
    }

    vscode = json.loads(install_module._render_vscode_mcp_json(target, servers).decode())
    v_args = vscode["servers"]["workstate-handoff-mcp"]["args"]
    assert v_args[:3] == ["run", "--no-sync", "--project"], v_args
    assert v_args.count("--no-sync") == 1, v_args

    codex = tomllib.loads(install_module._render_codex_config(target, servers).decode())
    c_args = codex["mcp_servers"]["workstate-handoff-mcp"]["args"]
    assert c_args[:3] == ["run", "--no-sync", "--project"], c_args
    assert c_args.count("--no-sync") == 1, c_args
