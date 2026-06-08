"""Opt-in adoption of stale local surfaces via repair (implementation note implementation note).

``repair(adopt_stale_local=[<surface>])`` re-materializes a surface that
doctor classified ``local_stale`` or ``local_redundant``: the local copy is
backed up under ``.workstate/backup/<ts>/<surface>`` first, the managed
symlink (git overlay source) is reinstated, and the receipt entry flips back
to ``source=shared`` so doctor stops classifying it. Never automatic, and
``local_override`` surfaces are excluded even when explicitly requested.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from workstate_bootstrap.install import BOOTSTRAP_MANIFEST_NAME, SCHEMA_VERSION
from workstate_bootstrap.subcommands import repair


SURFACE = "scripts/hooks"
GUARD_V1 = "#!/usr/bin/env python3\n# guard v1: ALT_ALLOW_BASH_MAIN_WRITE\n"
GUARD_V2 = "#!/usr/bin/env python3\n# guard v2: WORKSTATE bypass + git -C\n"
CONSUMER = "#!/usr/bin/env python3\n# consumer-authored hook, never shipped\n"


def _git(clone: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-C", str(clone), *argv],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _seed_clone_with_history(target: Path) -> Path:
    clone = target / ".workstate" / "remote"
    (clone / SURFACE).mkdir(parents=True)
    _git(clone, "init", "--quiet")
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    (clone / SURFACE / "guard.py").write_text(GUARD_V1)
    _git(clone, "add", "-A")
    _git(clone, "commit", "--quiet", "-m", "rev A")
    (clone / SURFACE / "guard.py").write_text(GUARD_V2)
    _git(clone, "add", "-A")
    _git(clone, "commit", "--quiet", "-m", "rev B")
    return clone


def _seed_ledger(target: Path, *, surfaces: list[dict[str, str]] | None = None) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "remote_url": "file:///tmp/fake.git",
        "remote_ref": "v0.1.23",
        "remote_sha": "0" * 40,
        "surfaces": (
            surfaces if surfaces is not None else [{"path": SURFACE, "source": "local"}]
        ),
        "configs": [],
        "mcp_servers": [],
    }
    (target / BOOTSTRAP_MANIFEST_NAME).write_text(json.dumps(payload, indent=2) + "\n")
    (target / ".task-state").mkdir(parents=True, exist_ok=True)
    (target / ".task-state" / "handoff.db").write_bytes(b"")


def _materialize_local(target: Path, content: str) -> None:
    local = target / SURFACE
    local.mkdir(parents=True)
    (local / "guard.py").write_text(content)


def test_adopt_stale_local_backs_up_and_relinks(tmp_path: Path) -> None:
    clone = _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path)
    _materialize_local(tmp_path, GUARD_V1)

    report = repair(target=tmp_path, adopt_stale_local=[SURFACE])

    adopted = [f for f in report["repaired"] if f["kind"] == "local_stale"]
    assert [f["path"] for f in adopted] == [SURFACE]

    link = tmp_path / SURFACE
    assert link.is_symlink(), "surface must be re-materialized as a symlink"
    assert link.resolve() == (clone / SURFACE).resolve()

    backups = list((tmp_path / ".workstate" / "backup").glob(f"*/{SURFACE}/guard.py"))
    assert len(backups) == 1, "local copy must be backed up before relinking"
    assert backups[0].read_text() == GUARD_V1

    manifest = json.loads((tmp_path / BOOTSTRAP_MANIFEST_NAME).read_text())
    assert manifest["surfaces"] == [{"path": SURFACE, "source": "shared"}]


def test_adopt_request_for_override_surface_is_refused(tmp_path: Path) -> None:
    _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path)
    _materialize_local(tmp_path, CONSUMER)

    report = repair(target=tmp_path, adopt_stale_local=[SURFACE])

    assert (tmp_path / SURFACE / "guard.py").read_text() == CONSUMER
    assert not (tmp_path / ".workstate" / "backup").exists()
    assert all(f["kind"] != "local_override" for f in report["repaired"])


def test_stale_local_not_adopted_without_explicit_request(tmp_path: Path) -> None:
    _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path)
    _materialize_local(tmp_path, GUARD_V1)

    report = repair(target=tmp_path)

    assert (tmp_path / SURFACE / "guard.py").read_text() == GUARD_V1
    assert all(f["kind"] != "local_stale" for f in report["repaired"])
    assert any(f["kind"] == "local_stale" for f in report["skipped"])


def test_cli_repair_passes_adopt_stale_local_flag(monkeypatch, tmp_path: Path) -> None:
    import workstate_bootstrap.cli as cli

    seen: dict[str, object] = {}

    def fake_repair(**kwargs):
        seen.update(kwargs)
        return {"repaired": [], "skipped": []}

    monkeypatch.setattr(cli, "repair", fake_repair)
    monkeypatch.setattr(cli, "_resolve_managed_servers", lambda *a, **k: None)

    rc = cli.main(
        [
            "repair",
            "--target",
            str(tmp_path),
            "--adopt-stale-local",
            SURFACE,
            "--adopt-stale-local",
            ".github/hooks",
        ]
    )

    assert rc == 0
    assert seen["adopt_stale_local"] == [SURFACE, ".github/hooks"]


def test_adopt_redundant_local_surface_relinks_and_flips_receipt(
    tmp_path: Path,
) -> None:
    """local_redundant (content already current) is the other adoption-eligible
    classification; it must take the same backup + relink + receipt-flip path."""
    clone = _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path)
    _materialize_local(tmp_path, GUARD_V2)

    report = repair(target=tmp_path, adopt_stale_local=[SURFACE])

    adopted = [f for f in report["repaired"] if f["kind"] == "local_redundant"]
    assert [f["path"] for f in adopted] == [SURFACE]
    link = tmp_path / SURFACE
    assert link.is_symlink()
    assert link.resolve() == (clone / SURFACE).resolve()
    manifest = json.loads((tmp_path / BOOTSTRAP_MANIFEST_NAME).read_text())
    assert manifest["surfaces"] == [{"path": SURFACE, "source": "shared"}]


CARVED = "Makefile.d"  # carve: evals.mk excluded, lifecycle.mk lifecycle-owned
TOOLS_V1 = "# tools v1\n"
TOOLS_V2 = "# tools v2\n"
LIFECYCLE_V1 = "# lifecycle v1\n"
LIFECYCLE_V2 = "# lifecycle v2\n"
EVALS_FRAGMENT = "# evals fragment: carve-excluded private tooling\n"


def test_adopt_carved_parent_rematerializes_carved_form(tmp_path: Path) -> None:
    """Adopting a carved surface must reproduce install's carved layout —
    real parent dir, per-child symlinks, lifecycle child copied as a real
    file, excluded child absent — never a whole-directory symlink that would
    re-expose evals.mk (review finding WS-DOCTOR-LOCAL-01-REVB-001)."""
    clone = tmp_path / ".workstate" / "remote"
    (clone / CARVED).mkdir(parents=True)
    _git(clone, "init", "--quiet")
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    (clone / CARVED / "tools.mk").write_text(TOOLS_V1)
    (clone / CARVED / "lifecycle.mk").write_text(LIFECYCLE_V1)
    (clone / CARVED / "evals.mk").write_text(EVALS_FRAGMENT)
    _git(clone, "add", "-A")
    _git(clone, "commit", "--quiet", "-m", "rev A")
    (clone / CARVED / "tools.mk").write_text(TOOLS_V2)
    (clone / CARVED / "lifecycle.mk").write_text(LIFECYCLE_V2)
    _git(clone, "add", "-A")
    _git(clone, "commit", "--quiet", "-m", "rev B")
    _seed_ledger(tmp_path, surfaces=[{"path": CARVED, "source": "local"}])
    local = tmp_path / CARVED
    local.mkdir(parents=True)
    (local / "tools.mk").write_text(TOOLS_V1)
    (local / "lifecycle.mk").write_text(LIFECYCLE_V1)
    (local / "evals.mk").write_text(EVALS_FRAGMENT)

    report = repair(target=tmp_path, adopt_stale_local=[CARVED])

    adopted = [f for f in report["repaired"] if f["kind"] == "local_stale"]
    assert [f["path"] for f in adopted] == [CARVED]

    parent = tmp_path / CARVED
    assert parent.is_dir() and not parent.is_symlink()
    tools = parent / "tools.mk"
    assert tools.is_symlink(), "non-excluded child must be a per-child symlink"
    assert tools.resolve() == (clone / CARVED / "tools.mk").resolve()
    lifecycle = parent / "lifecycle.mk"
    assert lifecycle.is_file() and not lifecycle.is_symlink()
    assert lifecycle.read_text() == LIFECYCLE_V2
    assert not (parent / "evals.mk").exists(), "carve-excluded child re-exposed"

    backups = list((tmp_path / ".workstate" / "backup").glob(f"*/{CARVED}/evals.mk"))
    assert len(backups) == 1, "full local copy (incl. excluded child) backed up"

    manifest = json.loads((tmp_path / BOOTSTRAP_MANIFEST_NAME).read_text())
    by_path = {e["path"]: e["source"] for e in manifest["surfaces"]}
    assert CARVED not in by_path, "parent-level local entry must be replaced"
    assert by_path[f"{CARVED}/tools.mk"] == "shared"
    assert by_path[f"{CARVED}/lifecycle.mk"] == "lifecycle"


def test_adopt_failure_mid_rematerialize_restores_backup(
    monkeypatch, tmp_path: Path
) -> None:
    """A failure between deleting the local copy and re-materializing must
    not strand the surface absent: the just-taken backup is restored and the
    finding reported as skipped (review finding WS-DOCTOR-LOCAL-01-REVA-002)."""
    _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path)
    _materialize_local(tmp_path, GUARD_V1)

    real_symlink_to = Path.symlink_to

    def failing_symlink_to(self: Path, *args: object, **kwargs: object) -> None:
        if self == tmp_path / SURFACE:
            raise OSError("simulated re-materialization failure")
        return real_symlink_to(self, *args, **kwargs)

    monkeypatch.setattr(Path, "symlink_to", failing_symlink_to)

    report = repair(target=tmp_path, adopt_stale_local=[SURFACE])

    local = tmp_path / SURFACE
    assert local.is_dir() and not local.is_symlink()
    assert (local / "guard.py").read_text() == GUARD_V1, "backup not restored"
    assert any(f["kind"] == "local_stale" for f in report["skipped"])
    assert all(f["kind"] != "local_stale" for f in report["repaired"])
    manifest = json.loads((tmp_path / BOOTSTRAP_MANIFEST_NAME).read_text())
    assert manifest["surfaces"] == [{"path": SURFACE, "source": "local"}]
