"""Tests for the implementation note Slice D runtime-path rename migration.

The runtime root ``.agentic/`` and the mirrored docs path ``docs/agentic/``
were renamed to ``.workstate/`` / ``docs/workstate/``. Bootstrap install/update
migrate a legacy checkout forward (archive-backed, idempotent) and doctor flags
a stale legacy tree. These tests pin that behavior at the unit level (the
``migrate_runtime_paths`` / ``plan_runtime_path_migration`` helpers) plus a
fresh-install / upgrade end-to-end check.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.test_install import (  # noqa: F401
    fake_remote,
    fake_remote_with_surfaces,
)
from tests.test_subcommands import SAMPLE_MCP_SERVERS


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
# unit: migrate_runtime_paths / plan_runtime_path_migration
# ---------------------------------------------------------------------------


def test_migration_moves_legacy_when_canonical_absent(tmp_path: Path) -> None:
    """A legacy tree with no canonical counterpart is renamed in place."""
    from workstate_bootstrap.install import migrate_runtime_paths

    target = tmp_path / "consumer"
    legacy_runtime = target / ".agentic"
    legacy_runtime.mkdir(parents=True)
    (legacy_runtime / "marker.txt").write_text("runtime state\n")
    legacy_docs = target / "docs" / "agentic"
    legacy_docs.mkdir(parents=True)
    (legacy_docs / "note.md").write_text("# mirror\n")

    moves = migrate_runtime_paths(target)

    assert {(m["legacy"], m["action"]) for m in moves} == {
        (".agentic", "move"),
        ("docs/agentic", "move"),
    }
    assert not legacy_runtime.exists()
    assert not legacy_docs.exists()
    assert (target / ".workstate" / "marker.txt").read_text() == "runtime state\n"
    assert (target / "docs" / "workstate" / "note.md").read_text() == "# mirror\n"


def test_migration_archives_legacy_when_both_present(tmp_path: Path) -> None:
    """When both legacy and canonical exist, the legacy tree is archived under
    a timestamped directory and the canonical tree is left untouched."""
    from workstate_bootstrap.install import migrate_runtime_paths

    target = tmp_path / "consumer"
    legacy_runtime = target / ".agentic"
    legacy_runtime.mkdir(parents=True)
    (legacy_runtime / "old.txt").write_text("legacy\n")
    canonical_runtime = target / ".workstate"
    canonical_runtime.mkdir(parents=True)
    (canonical_runtime / "new.txt").write_text("current\n")

    moves = migrate_runtime_paths(target, stamp="20260529T000000Z")

    runtime_move = next(m for m in moves if m["legacy"] == ".agentic")
    assert runtime_move["action"] == "archive"
    assert not legacy_runtime.exists()
    # Canonical tree is never overwritten.
    assert (canonical_runtime / "new.txt").read_text() == "current\n"
    # Legacy tree archived under the timestamped migration root.
    archived = (
        target
        / ".workstate"
        / "_migrated-from-agentic-20260529T000000Z"
        / ".agentic"
        / "old.txt"
    )
    assert archived.read_text() == "legacy\n"
    assert runtime_move["archived_to"].startswith(
        ".workstate/_migrated-from-agentic-20260529T000000Z"
    )


def test_migration_is_idempotent_and_noop_when_clean(tmp_path: Path) -> None:
    """A target with only canonical paths (or already migrated) is a no-op."""
    from workstate_bootstrap.install import migrate_runtime_paths

    target = tmp_path / "consumer"
    (target / ".workstate").mkdir(parents=True)
    (target / "docs" / "workstate").mkdir(parents=True)

    assert migrate_runtime_paths(target) == []
    # Re-running after a real migration is also a no-op.
    legacy = target / ".agentic"
    legacy.mkdir()
    (legacy / "x").write_text("x")
    migrate_runtime_paths(target, stamp="20260529T000000Z")
    assert migrate_runtime_paths(target) == []


def test_migration_dry_run_lists_moves_without_mutating(tmp_path: Path) -> None:
    """A dry-run reports the planned moves but touches nothing on disk."""
    from workstate_bootstrap.install import (
        migrate_runtime_paths,
        plan_runtime_path_migration,
    )

    target = tmp_path / "consumer"
    (target / ".agentic").mkdir(parents=True)

    planned = plan_runtime_path_migration(target)
    dry = migrate_runtime_paths(target, dry_run=True)

    assert planned == dry
    assert [m["legacy"] for m in dry] == [".agentic"]
    # Nothing moved.
    assert (target / ".agentic").exists()
    assert not (target / ".workstate").exists()


# ---------------------------------------------------------------------------
# install / doctor integration
# ---------------------------------------------------------------------------


def test_fresh_install_writes_only_workstate_paths(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """A fresh install materializes the runtime root + docs mirror only under
    the new ``.workstate/`` / ``docs/workstate/`` names — never the legacy
    ``.agentic/`` / ``docs/agentic/`` names."""
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

    assert (target / ".workstate" / "remote" / ".git").exists()
    assert not (target / ".agentic").exists()
    assert not (target / "docs" / "agentic").exists()


def test_upgrade_from_legacy_agentic_migrates_forward(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """Installing into a checkout that still carries a legacy ``.agentic/``
    tree migrates it forward before the clone runs. Because no ``.workstate/``
    exists yet at migration time, the legacy tree is *moved* (not orphaned or
    dropped) and the fresh clone lands inside the migrated runtime root."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    legacy = target / ".agentic"
    legacy.mkdir()
    (legacy / "legacy-state.txt").write_text("pre-rebrand\n")

    url, ref = fake_remote_with_surfaces
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    # New runtime root materialized with the clone; legacy gone from its old
    # location, its contents carried forward under .workstate/.
    assert (target / ".workstate" / "remote" / ".git").exists()
    assert not legacy.exists()
    assert not (target / "docs" / "agentic").exists()
    assert (target / ".workstate" / "legacy-state.txt").read_text() == "pre-rebrand\n"


def test_upgrade_archives_legacy_when_workstate_already_present(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """When a ``.workstate/`` runtime root already exists alongside a stale
    legacy ``.agentic/`` (a partially-migrated checkout), install archives the
    legacy tree rather than overwriting the canonical one."""
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    legacy = target / ".agentic"
    legacy.mkdir()
    (legacy / "legacy-state.txt").write_text("pre-rebrand\n")
    canonical = target / ".workstate"
    canonical.mkdir()
    (canonical / "keep.txt").write_text("current\n")

    url, ref = fake_remote_with_surfaces
    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        mcp_servers=SAMPLE_MCP_SERVERS,
    )

    assert (target / ".workstate" / "remote" / ".git").exists()
    assert (canonical / "keep.txt").read_text() == "current\n"
    assert not legacy.exists()
    archives = list(
        canonical.glob("_migrated-from-agentic-*/.agentic/legacy-state.txt")
    )
    assert archives, "legacy .agentic tree must be archived, not dropped"
    assert archives[0].read_text() == "pre-rebrand\n"


def test_doctor_flags_stale_legacy_agentic(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """A legacy ``.agentic/`` tree re-introduced after install is flagged by
    doctor as ``legacy_runtime_path`` drift."""
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

    # Re-introduce a stale legacy tree (e.g. a hand edit / un-migrated clone).
    stale = target / ".agentic"
    stale.mkdir()
    (stale / "leftover.txt").write_text("stale\n")

    findings = doctor(target=target)
    legacy = [f for f in findings if f["kind"] == "legacy_runtime_path"]
    assert legacy, "doctor must flag a stale legacy .agentic/ tree"
    assert legacy[0]["path"] == ".agentic"
    # Both paths now exist (.workstate from install + the stale .agentic), so
    # the planned action is an archive.
    assert legacy[0]["action"] == "archive"
