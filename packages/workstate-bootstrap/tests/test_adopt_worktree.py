"""TDD gate for implementation note Slice S1: ``adopt_worktree`` via the redirected materializer.

A linked worktree starts without the gitignored overlay. ``adopt_worktree``
re-runs the existing install materialization passes against the worktree, but
with ``clone = <primary>/.workstate/remote``, so the shared surfaces, the
lifecycle hoist, and the ``.workstate/remote`` clone redirect all "work out of
the box" with links pointing one hop at the primary's real clone.

Isolation is *safe-by-construction* (the leaner-invariant decision, see
`adopt-drop-runtime-allowlist-for-materializer-scope-invariant`): the materializer
only ever touches recorded surfaces + the clone, and ``.workstate/`` stays a real
local directory whose ``remote``/``generated`` children are the only symlinks. So
the per-worktree mutable set (``.task-state``, ``DASHBOARD.txt`` …) is never
adopted. These tests assert that as a set-membership guard rather than via a
runtime allow-list.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from workstate_bootstrap.adopt import OverlayNotMaterializedError, adopt_worktree

MARKER = ".workstate-bootstrap.json"

# Surfaces the synthetic primary clone ships. Mirrors install.SHARED_SURFACES;
# Makefile.d + scripts/workstate are carved (SURFACE_CHILD_EXCLUSIONS).
PLAIN_SURFACES = (".github/hooks", "docs/workstate/contracts", "docs/workstate/rules")

# Paths adopt must NEVER create in the worktree (per-worktree mutable state).
FORBIDDEN_IN_WORKTREE = (
    ".task-state",
    "DASHBOARD.txt",
    "CURRENT_TASK.json",
    ".mcp.json",
    ".workstate/state-backups",
    ".workstate/override-backups",
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


def _make_installed_primary(root: Path) -> tuple[Path, Path]:
    """Build a primary repo with a materialized overlay (clone + marker).

    Adopt reads surfaces from ``<primary>/.workstate/remote`` (a real clone-like
    directory), so the primary does not need its own materialized surfaces — only
    the clone content + the marker.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "--initial-branch=main", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "seed.txt").write_text("seed\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "seed", cwd=root)

    clone = root / ".workstate" / "remote"
    # Plain whole-dir surfaces.
    for surface in PLAIN_SURFACES:
        d = clone / surface
        d.mkdir(parents=True)
        (d / "MARKER.md").write_text(f"shared {surface}\n")
    # scripts/hooks (+ git hooks dir the hooksPath resolves against).
    hooks_git = clone / "scripts" / "hooks" / "git"
    hooks_git.mkdir(parents=True)
    (hooks_git / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    # Carved surface: Makefile.d ships a normal child, the hoisted lifecycle.mk,
    # and the carved-out evals.mk.
    mkd = clone / "Makefile.d"
    mkd.mkdir(parents=True)
    (mkd / "common.mk").write_text("# common\n")
    (mkd / "lifecycle.mk").write_text("# lifecycle targets\n")
    (mkd / "evals.mk").write_text("# evals (carved out)\n")
    # Carved surface: scripts/workstate ships the hoisted lifecycle/ dir, a normal
    # child, and the carved-out evals/ dir.
    sw = clone / "scripts" / "workstate"
    (sw / "lifecycle").mkdir(parents=True)
    (sw / "lifecycle" / "runner.py").write_text("# runner\n")
    (sw / "other.py").write_text("# other\n")
    (sw / "evals").mkdir(parents=True)
    (sw / "evals" / "x.py").write_text("# evals (carved out)\n")
    # A primary generated plugin tree, to be child-symlinked.
    gen = root / ".workstate" / "generated"
    gen.mkdir(parents=True)
    (gen / "PLUGINS.md").write_text("generated plugin tree\n")

    (root / MARKER).write_text("{}\n")
    return root, clone


def _add_worktree(primary: Path, wt: Path) -> Path:
    _git("worktree", "add", str(wt), cwd=primary)
    return wt


@pytest.fixture()
def primary(tmp_path: Path) -> Path:
    root, _clone = _make_installed_primary(tmp_path / "primary")
    return root


# ---------------------------------------------------------------------------
# Core adoption: redirected materializer
# ---------------------------------------------------------------------------


def test_adopt_redirects_clone_as_child_symlink(tmp_path: Path, primary: Path) -> None:
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)

    ws = wt / ".workstate"
    assert ws.is_dir() and not ws.is_symlink(), ".workstate must stay a real local dir"
    remote = ws / "remote"
    assert remote.is_symlink()
    assert remote.resolve() == (primary / ".workstate" / "remote").resolve()


def test_adopt_generated_child_symlink(tmp_path: Path, primary: Path) -> None:
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)
    gen = wt / ".workstate" / "generated"
    assert gen.is_symlink()
    assert gen.resolve() == (primary / ".workstate" / "generated").resolve()


def test_adopt_plain_surface_links_one_hop_to_primary(
    tmp_path: Path, primary: Path
) -> None:
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)

    rules = wt / "docs" / "workstate" / "rules"
    assert rules.is_symlink()
    # One hop: resolves to the PRIMARY's real clone, not through the wt's own
    # .workstate/remote symlink.
    assert (
        rules.resolve()
        == (
            primary / ".workstate" / "remote" / "docs" / "workstate" / "rules"
        ).resolve()
    )
    raw = os.readlink(rules)
    assert not os.path.isabs(raw), "link should be relative for relocation safety"
    # The link must not route through the worktree's own .workstate.
    assert ".workstate/remote" not in raw or str(primary.name) in raw


def test_adopt_carved_makefile_d_with_lifecycle_hoist(
    tmp_path: Path, primary: Path
) -> None:
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)

    mkd = wt / "Makefile.d"
    assert mkd.is_dir() and not mkd.is_symlink(), "carved surface is a real dir"
    # Normal child: symlink into the primary clone.
    assert (mkd / "common.mk").is_symlink()
    # lifecycle.mk: hoisted as a REAL file, not a symlink.
    assert (mkd / "lifecycle.mk").is_file()
    assert not (mkd / "lifecycle.mk").is_symlink()
    # evals.mk: carved out (excluded child).
    assert not (mkd / "evals.mk").exists()


def test_adopt_carved_scripts_workstate(tmp_path: Path, primary: Path) -> None:
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)

    sw = wt / "scripts" / "workstate"
    assert sw.is_dir() and not sw.is_symlink()
    # lifecycle/ hoisted as a real dir with content.
    assert (sw / "lifecycle").is_dir()
    assert (sw / "lifecycle" / "runner.py").is_file()
    # evals/ carved out.
    assert not (sw / "evals").exists()


def test_adopt_injects_lifecycle_makefile_include(
    tmp_path: Path, primary: Path
) -> None:
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)
    makefile = (wt / "Makefile").read_text()
    assert "WORKSTATE_BOOTSTRAP LIFECYCLE INCLUDE" in makefile
    assert "-include Makefile.d/*.mk" in makefile


def test_adopt_sets_git_hooks_path(tmp_path: Path, primary: Path) -> None:
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)
    assert _git("config", "core.hooksPath", cwd=wt) == "scripts/hooks/git"


# ---------------------------------------------------------------------------
# Resolution / guards / no-ops
# ---------------------------------------------------------------------------


def test_adopt_auto_resolves_primary_from_marker(tmp_path: Path, primary: Path) -> None:
    wt = _add_worktree(primary, tmp_path / "wt")
    receipt = adopt_worktree(target=wt)  # no explicit --primary
    assert receipt["adopted"] is True
    assert (wt / "docs" / "workstate" / "rules").is_symlink()


def test_adopt_on_primary_is_noop(tmp_path: Path, primary: Path) -> None:
    receipt = adopt_worktree(target=primary, primary=primary)
    assert receipt["adopted"] is False
    assert receipt["reason"] == "not_a_linked_worktree"


def test_adopt_fails_loudly_when_primary_not_materialized(tmp_path: Path) -> None:
    bare_primary = tmp_path / "bare-primary"
    bare_primary.mkdir()
    _git("init", "--initial-branch=main", cwd=bare_primary)
    _git("config", "user.email", "t@e.com", cwd=bare_primary)
    _git("config", "user.name", "T", cwd=bare_primary)
    (bare_primary / "seed.txt").write_text("x\n")
    _git("add", "-A", cwd=bare_primary)
    _git("commit", "-m", "seed", cwd=bare_primary)
    wt = _add_worktree(bare_primary, tmp_path / "wt")
    with pytest.raises(OverlayNotMaterializedError):
        adopt_worktree(target=wt, primary=bare_primary)


# ---------------------------------------------------------------------------
# Idempotency + foreign-file preservation
# ---------------------------------------------------------------------------


def test_adopt_is_idempotent(tmp_path: Path, primary: Path) -> None:
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)
    adopt_worktree(target=wt, primary=primary)  # must not raise
    assert (wt / "Makefile.d" / "common.mk").is_symlink()
    assert (wt / "Makefile.d" / "lifecycle.mk").is_file()
    assert not (wt / "Makefile.d" / "lifecycle.mk").is_symlink()


def test_adopt_preserves_foreign_local_surface(tmp_path: Path, primary: Path) -> None:
    wt = _add_worktree(primary, tmp_path / "wt")
    # Operator placed a real local surface dir before adoption.
    local = wt / "docs" / "workstate" / "rules"
    local.mkdir(parents=True)
    (local / "LOCAL.md").write_text("operator content\n")
    adopt_worktree(target=wt, primary=primary)
    assert not local.is_symlink(), "foreign local content must win (overlay precedence)"
    assert (local / "LOCAL.md").read_text() == "operator content\n"


# ---------------------------------------------------------------------------
# --check drift mode
# ---------------------------------------------------------------------------


def test_check_reports_drift_before_adopt(tmp_path: Path, primary: Path) -> None:
    wt = _add_worktree(primary, tmp_path / "wt")
    receipt = adopt_worktree(target=wt, primary=primary, check=True)
    assert receipt["ok"] is False
    assert receipt["drift"], "expected drift entries for an unadopted worktree"
    # check mode must not write.
    assert not (wt / "docs" / "workstate" / "rules").exists()


def test_check_clean_after_adopt(tmp_path: Path, primary: Path) -> None:
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)
    receipt = adopt_worktree(target=wt, primary=primary, check=True)
    assert receipt["ok"] is True
    assert not receipt["drift"]


# ---------------------------------------------------------------------------
# Isolation (set-membership) + cross-worktree contamination + teardown
# ---------------------------------------------------------------------------


def test_adopt_does_not_create_per_worktree_mutable_state(
    tmp_path: Path, primary: Path
) -> None:
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)
    for rel in FORBIDDEN_IN_WORKTREE:
        assert not (wt / rel).exists(), f"adopt must not create {rel}"


def test_task_state_is_per_worktree(tmp_path: Path, primary: Path) -> None:
    wt_a = _add_worktree(primary, tmp_path / "wt-a")
    wt_b = _add_worktree(primary, tmp_path / "wt-b")
    adopt_worktree(target=wt_a, primary=primary)
    adopt_worktree(target=wt_b, primary=primary)
    # A writes local task state.
    (wt_a / ".task-state").mkdir()
    (wt_a / ".task-state" / "handoff.db").write_text("A\n")
    # It must be invisible in B and in the primary.
    assert not (wt_b / ".task-state").exists()
    assert not (primary / ".task-state").exists()


def test_adopted_worktree_can_be_removed(tmp_path: Path, primary: Path) -> None:
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)
    # git worktree remove must not be blocked by the adopted symlinks.
    _git("worktree", "remove", "--force", str(wt), cwd=primary)
    assert not wt.exists()


# ---------------------------------------------------------------------------
# S1b review remediation: drift correctness, hooksPath isolation, edge guards
# ---------------------------------------------------------------------------


def test_check_reports_drift_when_carved_child_removed(
    tmp_path: Path, primary: Path
) -> None:
    """--check must inspect carved-surface children, not just the lifecycle hoists."""
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)
    # Remove a non-excluded carved child symlink (Makefile.d/common.mk).
    (wt / "Makefile.d" / "common.mk").unlink()
    receipt = adopt_worktree(target=wt, primary=primary, check=True)
    assert receipt["ok"] is False
    assert any("Makefile.d" in entry for entry in receipt["drift"]), receipt["drift"]


def test_check_clean_with_foreign_preserved_surface(
    tmp_path: Path, primary: Path
) -> None:
    """A foreign-preserved local surface must NOT be reported as perpetual drift."""
    wt = _add_worktree(primary, tmp_path / "wt")
    local = wt / "docs" / "workstate" / "rules"
    local.mkdir(parents=True)
    (local / "LOCAL.md").write_text("operator\n")
    adopt_worktree(target=wt, primary=primary)
    receipt = adopt_worktree(target=wt, primary=primary, check=True)
    assert receipt["ok"] is True, (
        f"foreign-preserved surface must not be drift: {receipt['drift']}"
    )


def test_adopt_does_not_change_primary_hooks_path(
    tmp_path: Path, primary: Path
) -> None:
    """Adopting a worktree must not mutate the primary's (shared) core.hooksPath."""
    wt = _add_worktree(primary, tmp_path / "wt")
    before = subprocess.run(
        ["git", "config", "core.hooksPath"],
        cwd=str(primary),
        capture_output=True,
        text=True,
    )
    adopt_worktree(target=wt, primary=primary)
    after = subprocess.run(
        ["git", "config", "core.hooksPath"],
        cwd=str(primary),
        capture_output=True,
        text=True,
    )
    assert (after.returncode, after.stdout) == (before.returncode, before.stdout), (
        "primary core.hooksPath must be unchanged after adopting a worktree"
    )
    assert _git("config", "core.hooksPath", cwd=wt) == "scripts/hooks/git"


def test_adopt_heals_preexisting_workstate_symlink(
    tmp_path: Path, primary: Path
) -> None:
    """A pre-existing .workstate SYMLINK must be replaced by a real local dir.

    Otherwise the remote/generated child links would be written THROUGH the
    symlink into whatever it points at (cross-worktree contamination).
    """
    wt = _add_worktree(primary, tmp_path / "wt")
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (wt / ".workstate").symlink_to(decoy)
    adopt_worktree(target=wt, primary=primary)
    ws = wt / ".workstate"
    assert ws.is_dir() and not ws.is_symlink()
    assert (ws / "remote").resolve() == (primary / ".workstate" / "remote").resolve()
    # Nothing must have been written through the old symlink into the decoy.
    assert not (decoy / "remote").exists()


def test_marketplace_source_resolves_through_generated_symlink(
    tmp_path: Path, primary: Path
) -> None:
    """The tracked marketplace's relative source must resolve via the adopted
    .workstate/generated symlink (so the plugin registers without adopt copying
    marketplace.json)."""
    plugin_dir = (
        primary
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "base"
        / "claude"
        / "skills"
    )
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "SKILL.md").write_text("# skill\n")
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)
    resolved = (
        wt
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "base"
        / "claude"
        / "skills"
        / "SKILL.md"
    )
    assert resolved.exists(), "marketplace source path must resolve through the symlink"


def test_adopt_does_not_materialize_claude_plugin_marketplace(
    tmp_path: Path, primary: Path
) -> None:
    """implementation note S2 contract (as-built decision #114): adopt resolves the plugin
    via the *tracked* ``.claude-plugin/marketplace.json`` (whose relative source
    points into the adopt-symlinked ``.workstate/generated``) and does NOT itself
    create or copy ``.claude-plugin/marketplace.json``. Consumers that gitignore
    ``.claude-plugin`` need a separate install pass (explicitly out of scope).
    """
    # The synthetic primary ships no .claude-plugin (matches a worktree that
    # inherits only tracked files — adopt is the unit under test here).
    assert not (primary / ".claude-plugin").exists()
    wt = _add_worktree(primary, tmp_path / "wt")

    receipt = adopt_worktree(target=wt, primary=primary)

    # adopt must NOT materialize a .claude-plugin surface...
    assert not (wt / ".claude-plugin" / "marketplace.json").exists()
    assert not (wt / ".claude-plugin").exists()
    assert not any(
        str(entry["path"]).startswith(".claude-plugin") for entry in receipt["surfaces"]
    ), "adopt must not record a .claude-plugin surface"
    # ...but the generated tree the tracked marketplace resolves through IS
    # symlinked, so a real consumer's tracked marketplace.json still registers.
    assert (wt / ".workstate" / "generated").is_symlink()


# ---------------------------------------------------------------------------
# Round-2 remediation: worktree-scoped hooksPath + gitignore drift
# ---------------------------------------------------------------------------


def test_set_git_hooks_path_is_worktree_scoped(tmp_path: Path, primary: Path) -> None:
    """install._set_git_hooks_path must scope the write to a linked worktree
    (via --worktree) so repair/adopt never mutate the primary's shared config."""
    from workstate_bootstrap.install import _set_git_hooks_path

    wt = _add_worktree(primary, tmp_path / "wt")
    before = subprocess.run(
        ["git", "config", "core.hooksPath"],
        cwd=str(primary),
        capture_output=True,
        text=True,
    )
    entry = _set_git_hooks_path(wt)
    assert entry is not None and entry.get("scope") == "worktree"
    after = subprocess.run(
        ["git", "config", "core.hooksPath"],
        cwd=str(primary),
        capture_output=True,
        text=True,
    )
    assert (after.returncode, after.stdout) == (before.returncode, before.stdout), (
        "primary core.hooksPath must be unchanged by a worktree-scoped write"
    )
    assert _git("config", "core.hooksPath", cwd=wt) == "scripts/hooks/git"


def test_check_reports_drift_when_gitignore_block_removed(
    tmp_path: Path, primary: Path
) -> None:
    """--check must flag a missing managed .gitignore block (apply writes it)."""
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)
    (wt / ".gitignore").unlink()
    receipt = adopt_worktree(target=wt, primary=primary, check=True)
    assert receipt["ok"] is False
    assert ".gitignore" in receipt["drift"]


def test_adopt_self_hosting_worktree_does_not_touch_tracked_gitignore(
    tmp_path: Path,
) -> None:
    """Self-hosting source repo (the workstate monorepo): a feature worktree
    inherits a TRACKED ``.gitignore`` that already ignores the overlay surfaces.
    Adopt must NOT append the managed block — doing so dirties every feature
    worktree's tracked ``.gitignore`` (and its root-anchored patterns would ignore
    the repo's own tracked source). ``--check`` must agree it is clean, not report
    perpetual, unsatisfiable ``.gitignore`` drift.
    """
    primary, _clone = _make_installed_primary(tmp_path / "primary")
    # The self-hosting primary tracks a hand-authored .gitignore covering the
    # runtime dirs + the overlay surfaces (a mix of root-anchored and
    # directory-only patterns, as the live monorepo ships).
    (primary / ".gitignore").write_text(
        ".workstate/\n"
        ".task-state/\n"
        "/scripts/hooks\n"
        "/.github/hooks\n"
        "/docs/workstate/contracts\n"
        "/docs/workstate/rules\n"
        "/Makefile.d\n"
        "/scripts/workstate\n"
        "/.github/prompts/\n"
        "/.codex/hooks.json\n"
    )
    _git("add", ".gitignore", cwd=primary)
    _git("commit", "-m", "hand-authored overlay ignores", cwd=primary)

    wt = _add_worktree(primary, tmp_path / "wt")
    tracked_before = (wt / ".gitignore").read_text()

    receipt = adopt_worktree(target=wt, primary=primary)

    assert receipt["adopted"] is True
    assert receipt["gitignore"]["action"] == "skipped_self_managed"
    # The tracked .gitignore is byte-for-byte untouched — no managed block, no
    # spurious working-tree modification.
    assert (wt / ".gitignore").read_text() == tracked_before
    assert "WORKSTATE_BOOTSTRAP OVERLAY IGNORE" not in tracked_before
    gitignore_status = [
        line
        for line in _git("status", "--porcelain", cwd=wt).splitlines()
        if ".gitignore" in line
    ]
    assert gitignore_status == [], gitignore_status

    # --check must not report .gitignore as drift (apply skipped it on purpose).
    check = adopt_worktree(target=wt, primary=primary, check=True)
    assert ".gitignore" not in check["drift"], check["drift"]


# ---------------------------------------------------------------------------
# implementation note S1: apply and --check share ONE surface enumeration
#
# The two round-1 HIGH false-drift bugs came from adopt._compute_drift
# hand-reimplementing the materializer's carve/exclusion walk. The fix is a
# single shared enumeration (install.iter_expected_surface_targets) that both
# the materializer (apply) and the drift guard (--check) consume, so they
# cannot desync. (finding revB-install-private-symbol-coupling)
# ---------------------------------------------------------------------------


def test_apply_and_check_share_one_surface_enumeration(
    tmp_path: Path, primary: Path
) -> None:
    # The package re-exports the install *function*, shadowing the submodule
    # under attribute access — resolve the module explicitly (see adopt.py's shim).
    import importlib

    install = importlib.import_module("workstate_bootstrap.install")
    from workstate_bootstrap.adopt import _compute_drift

    clone = primary / ".workstate" / "remote"
    wt = _add_worktree(primary, tmp_path / "wt")

    # The shared enumeration: carved children that ship are listed; excluded /
    # lifecycle-hoisted children are not. This is the single source of the
    # carve/exclusion rule.
    expected = {t.rel for t in install.iter_expected_surface_targets(wt, clone)}
    assert "Makefile.d/common.mk" in expected
    assert "Makefile.d/evals.mk" not in expected  # SURFACE_CHILD_EXCLUSIONS
    assert "Makefile.d/lifecycle.mk" not in expected  # LIFECYCLE_HOISTS owns it
    assert "scripts/workstate/other.py" in expected
    assert "scripts/workstate/evals" not in expected
    for plain in PLAIN_SURFACES:
        assert plain in expected

    # Apply is driven by the same enumeration: every bootstrap-owned ("shared")
    # symlink the materializer records on a clean worktree is exactly an
    # enumerated target — no extra, none missing.
    materialized = install._materialize_surfaces(wt, clone)
    shared = {e["path"] for e in materialized if e["source"] == "shared"}
    assert shared == expected

    # And --check inspects exactly that same set: clean right after a full
    # adopt, and removing an enumerated surface target surfaces precisely that
    # target as drift — both the carved-child branch and the plain-surface
    # branch (not more, not less).
    adopt_worktree(target=wt, primary=primary)
    assert _compute_drift(wt, primary, clone) == []
    (wt / "Makefile.d" / "common.mk").unlink()  # carved child
    assert _compute_drift(wt, primary, clone) == ["Makefile.d/common.mk"]
    (wt / "docs" / "workstate" / "rules").unlink()  # plain whole-dir surface
    assert _compute_drift(wt, primary, clone) == [
        "docs/workstate/rules",
        "Makefile.d/common.mk",
    ]


def test_foreign_carved_parent_collapses_to_one_local_entry(
    tmp_path: Path, primary: Path
) -> None:
    """Apply-side parity for the carved_parent_is_foreign path: a FOREIGN carved
    surface parent (a symlink not pointing into our clone) wins by local
    precedence — `_materialize_surfaces` records exactly one {surface,'local'}
    entry and symlinks NONE of its children, while other carved surfaces still
    materialize normally."""
    import importlib

    install = importlib.import_module("workstate_bootstrap.install")

    clone = primary / ".workstate" / "remote"
    wt = _add_worktree(primary, tmp_path / "wt")

    # A foreign symlink at the carved surface, pointing outside the clone.
    outside = tmp_path / "operator_makefile_d"
    outside.mkdir()
    (outside / "local.mk").write_text("# operator-local\n")
    (wt / "Makefile.d").symlink_to(os.path.relpath(outside, wt))

    entries = install._materialize_surfaces(wt, clone)

    mkd_entries = [e for e in entries if e["path"].startswith("Makefile.d")]
    assert mkd_entries == [{"path": "Makefile.d", "source": "local"}]
    # The foreign symlink is untouched; no bootstrap-owned child links created.
    assert (wt / "Makefile.d").is_symlink()
    assert (wt / "Makefile.d").resolve() == outside.resolve()
    # The other carved surface is unaffected (children still materialize).
    assert (wt / "scripts" / "workstate" / "other.py").is_symlink()


# ---------------------------------------------------------------------------
# implementation note S3b: relocation repoint — a dangling bootstrap-owned link is
# repointed (revB-relocation-dangling-symlink-no-repoint); a dangling FOREIGN
# link keeps local precedence.
# ---------------------------------------------------------------------------


def test_adopt_repoints_dangling_relocated_bootstrap_link(
    tmp_path: Path, primary: Path
) -> None:
    clone = primary / ".workstate" / "remote"
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)

    link = wt / ".github" / "hooks"  # a plain whole-dir surface
    assert link.is_symlink() and link.exists()
    link.unlink()
    # Stale pointer naming a .workstate/remote clone that no longer exists here
    # (as left behind by a relocated primary).
    link.symlink_to("../../relocated-primary/.workstate/remote/.github/hooks")
    assert not link.exists()  # dangling

    # --check flags it as drift (apply and check agree it is repointable)...
    receipt = adopt_worktree(target=wt, primary=primary, check=True)
    assert ".github/hooks" in receipt["drift"]
    # ...and re-adopt repoints it to the live clone.
    adopt_worktree(target=wt, primary=primary)
    assert link.is_symlink() and link.exists()
    assert link.resolve() == (clone / ".github" / "hooks").resolve()


def test_adopt_leaves_dangling_foreign_link_untouched(
    tmp_path: Path, primary: Path
) -> None:
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)
    link = wt / ".github" / "hooks"
    link.unlink()
    # A dangling link whose stale target does NOT name our clone subtree is the
    # operator's own broken link — local precedence, never clobbered.
    link.symlink_to("../../operator-target/elsewhere")
    assert not link.exists()
    receipt = adopt_worktree(target=wt, primary=primary, check=True)
    assert ".github/hooks" not in receipt["drift"]
    adopt_worktree(target=wt, primary=primary)
    assert os.readlink(link) == "../../operator-target/elsewhere"


def test_adopt_leaves_dangling_link_with_clone_substring_untouched(
    tmp_path: Path, primary: Path
) -> None:
    """The dangling-repoint heuristic is segment-anchored: a foreign link whose
    target merely CONTAINS ``.workstate/remote`` as a substring (e.g.
    ``.workstate/remote-backup``) is NOT a bootstrap-owned clone link and must be
    left untouched (revC-dangling-substring-unanchored)."""
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)
    link = wt / ".github" / "hooks"
    link.unlink()
    link.symlink_to("../../op/.workstate/remote-backup/.github/hooks")  # substring only
    assert not link.exists()
    receipt = adopt_worktree(target=wt, primary=primary, check=True)
    assert ".github/hooks" not in receipt["drift"]
    adopt_worktree(target=wt, primary=primary)
    assert os.readlink(link) == "../../op/.workstate/remote-backup/.github/hooks"
