"""implementation note implementation note: install/update auto-adopt ``local_redundant`` surfaces.

The 2026-06-05 consumer incident: after a package-source refresh, five
surfaces whose local content was byte-identical to the current payload
(``local_redundant``) each required a manual
``repair --adopt-stale-local <path>`` pass. Adoption of an *identical* copy
is provably safe — only the materialization mode / receipt ``source`` flips —
so ``update`` now adopts them automatically (default on,
``--no-adopt-redundant`` opt-out) via :func:`_adopt_redundant_surfaces`.
``local_stale`` stays opt-in and ``local_override`` (divergent content) is
never touched (implementation note semantics unchanged).

Fixtures reuse the implementation note seeded-ledger shape: a real git clone at
``.workstate/remote`` whose payload history is rev A (v1) -> HEAD (v2).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from workstate_bootstrap.install import BOOTSTRAP_MANIFEST_NAME, SCHEMA_VERSION
from workstate_bootstrap.subcommands import _adopt_redundant_surfaces


SURFACE = "scripts/hooks"
GUARD_V1 = "#!/usr/bin/env python3\n# guard v1: legacy bypass\n"
GUARD_V2 = "#!/usr/bin/env python3\n# guard v2: current payload\n"
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


def _seed_ledger(target: Path) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "remote_url": "file:///tmp/fake.git",
        "remote_ref": "v0.1.23",
        "remote_sha": "0" * 40,
        "surfaces": [{"path": SURFACE, "source": "local"}],
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


def _surface_sources(target: Path) -> dict[str, str]:
    manifest = json.loads((target / BOOTSTRAP_MANIFEST_NAME).read_text())
    return {
        entry["path"]: entry["source"]
        for entry in manifest["surfaces"]
        if isinstance(entry, dict)
    }


def test_adopts_identical_local_surface_with_backup(tmp_path: Path) -> None:
    _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path)
    _materialize_local(tmp_path, GUARD_V2)  # identical to HEAD -> local_redundant

    adopted = _adopt_redundant_surfaces(tmp_path)

    assert adopted == [SURFACE]
    sources = _surface_sources(tmp_path)
    assert all(
        source == "shared"
        for path, source in sources.items()
        if path == SURFACE or path.startswith(SURFACE + "/")
    ), sources
    backups = list((tmp_path / ".workstate" / "backup").glob("*/scripts/hooks"))
    assert backups, "adoption must back the local copy up first"


def test_stale_local_surface_is_not_auto_adopted(tmp_path: Path) -> None:
    _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path)
    _materialize_local(tmp_path, GUARD_V1)  # older revision -> local_stale

    adopted = _adopt_redundant_surfaces(tmp_path)

    assert adopted == []
    assert _surface_sources(tmp_path)[SURFACE] == "local"
    assert (tmp_path / SURFACE / "guard.py").read_text() == GUARD_V1


def test_consumer_override_is_never_touched(tmp_path: Path) -> None:
    _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path)
    _materialize_local(tmp_path, CONSUMER)  # matches no revision -> local_override

    adopted = _adopt_redundant_surfaces(tmp_path)

    assert adopted == []
    assert _surface_sources(tmp_path)[SURFACE] == "local"
    assert (tmp_path / SURFACE / "guard.py").read_text() == CONSUMER


def test_update_runs_adoption_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """update() wiring: adoption runs post-install by default and lands in the
    result; ``adopt_redundant=False`` skips it entirely."""
    import importlib

    install_mod = importlib.import_module("workstate_bootstrap.install")
    subcommands_mod = importlib.import_module("workstate_bootstrap.subcommands")
    from workstate_bootstrap.subcommands import update

    target = tmp_path / "consumer"
    target.mkdir()
    (target / BOOTSTRAP_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "remote_url": "file:///tmp/fake.git",
                "remote_ref": "v1",
                "remote_sha": "0" * 40,
                "surfaces": [],
                "configs": [],
                "mcp_servers": [],
            }
        )
    )

    monkeypatch.setattr(
        install_mod,
        "install",
        lambda **kwargs: {"remote_ref": "v2", "remote_sha": "1" * 40},
    )
    calls: list[Path] = []

    def fake_adopt(target_path: Path) -> list[str]:
        calls.append(target_path)
        return ["scripts/hooks"]

    monkeypatch.setattr(subcommands_mod, "_adopt_redundant_surfaces", fake_adopt)

    result = update(target=target, remote_ref="v2")
    assert calls == [target.resolve()]
    assert result["adopted_redundant"] == ["scripts/hooks"]

    calls.clear()
    result = update(target=target, remote_ref="v2", adopt_redundant=False)
    assert calls == []
    assert result["adopted_redundant"] == []


def test_update_keeps_transient_backup_paths_after_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adoption re-reads the on-disk manifest, which never carries install()'s
    transient backup keys — they must survive into the returned manifest or the
    CLI's "override backup:" line silently disappears."""
    import importlib

    install_mod = importlib.import_module("workstate_bootstrap.install")
    subcommands_mod = importlib.import_module("workstate_bootstrap.subcommands")
    from workstate_bootstrap.subcommands import update

    target = tmp_path / "consumer"
    target.mkdir()
    (target / BOOTSTRAP_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "remote_url": "file:///tmp/fake.git",
                "remote_ref": "v1",
                "remote_sha": "0" * 40,
                "surfaces": [],
                "configs": [],
                "mcp_servers": [],
            }
        )
    )

    monkeypatch.setattr(
        install_mod,
        "install",
        lambda **kwargs: {
            "remote_ref": "v2",
            "remote_sha": "1" * 40,
            "override_backup_path": "/tmp/override-backup",
            "state_backup_path": "/tmp/state-backup",
        },
    )
    monkeypatch.setattr(
        subcommands_mod, "_adopt_redundant_surfaces", lambda _t: ["scripts/hooks"]
    )

    result = update(target=target, remote_ref="v2")

    assert result["adopted_redundant"] == ["scripts/hooks"]
    assert result["override_backup_path"] == "/tmp/override-backup"
    assert result["state_backup_path"] == "/tmp/state-backup"


def test_cli_exposes_no_adopt_redundant_flag() -> None:
    from workstate_bootstrap.cli import _build_parser

    update_args = _build_parser().parse_args(
        ["update", "--target", ".", "--no-adopt-redundant"]
    )
    assert update_args.no_adopt_redundant is True

    default_args = _build_parser().parse_args(["update", "--target", "."])
    assert default_args.no_adopt_redundant is False
