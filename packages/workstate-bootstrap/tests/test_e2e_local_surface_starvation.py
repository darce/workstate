"""End-to-end replay of the 2026-06-04 update-starvation incident (implementation note
implementation note).

A consumer's ``scripts/hooks`` became a real-file copy of an old release
(pre-symlink materialization). Installing a newer ref succeeded, the receipt
bumped ``remote_ref``, and doctor said "no drift detected" — while the guard
code on disk stayed two releases old. These tests pin the whole pipeline:

- install at the new ref names the local-precedence skip (never silent),
- doctor flags the stale copy as ``local_stale`` even though the receipt
  claims the new ref (receipt bump alone is insufficient for doctor-clean),
- consumer-authored content is classified ``local_override`` and never
  auto-replaced,
- ``repair --adopt-stale-local`` recovers the incident state,
- the package source (no clone history) falls back to ``local_override`` for
  divergent content while still detecting ``local_redundant``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from workstate_bootstrap.cli import main
from workstate_bootstrap.install import BOOTSTRAP_MANIFEST_NAME, SCHEMA_VERSION, install
from workstate_bootstrap.subcommands import doctor, repair


SURFACE = "scripts/hooks"
GUARD_V1 = "#!/usr/bin/env python3\n# guard v1: ALT_ALLOW_BASH_MAIN_WRITE\n"
GUARD_V2 = "#!/usr/bin/env python3\n# guard v2: WORKSTATE bypass + git -C\n"
CONSUMER = "#!/usr/bin/env python3\n# consumer-authored hook, never shipped\n"


def _git(cwd: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *argv], check=True, capture_output=True, text=True
    )


@pytest.fixture()
def two_release_remote(tmp_path: Path) -> str:
    """Bare remote with tags v1 (GUARD_V1) and v2 (GUARD_V2) shipping
    ``scripts/hooks``."""
    src = tmp_path / "remote-src"
    (src / SURFACE).mkdir(parents=True)
    _git(src, "init", "--initial-branch=main")
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "t")
    (src / SURFACE / "guard.py").write_text(GUARD_V1)
    _git(src, "add", "-A")
    _git(src, "commit", "-m", "release v1")
    _git(src, "tag", "v1")
    (src / SURFACE / "guard.py").write_text(GUARD_V2)
    _git(src, "add", "-A")
    _git(src, "commit", "-m", "release v2")
    _git(src, "tag", "v2")
    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(src), str(bare))
    return f"file://{bare}"


def _install_v1_then_degrade_to_real_files(target: Path, remote_url: str) -> None:
    """Install v1, then replace the managed symlink with a real-file copy of
    the v1 payload — the incident's starting state."""
    target.mkdir(exist_ok=True)
    install(target=target, remote_url=remote_url, remote_ref="v1")
    link = target / SURFACE
    assert link.is_symlink(), "precondition: v1 install materialized a symlink"
    link.unlink()
    link.mkdir(parents=True)
    (link / "guard.py").write_text(GUARD_V1)


def test_incident_replay_install_names_skip_and_doctor_flags_stale(
    tmp_path: Path, two_release_remote: str, capsys: pytest.CaptureFixture
) -> None:
    target = tmp_path / "consumer"
    _install_v1_then_degrade_to_real_files(target, two_release_remote)

    rc = main(
        [
            "install",
            "--remote-url",
            two_release_remote,
            "--remote-ref",
            "v2",
            "--target",
            str(target),
            "--no-mcp-servers",
        ]
    )
    out = capsys.readouterr().out

    assert rc == 0
    # Never silent: the skip is named in the install output.
    assert f"skipped (local precedence): {SURFACE}" in out
    assert "1 surface(s) kept under local precedence." in out

    # Receipt claims v2…
    manifest = json.loads((target / BOOTSTRAP_MANIFEST_NAME).read_text())
    assert manifest["remote_ref"] == "v2"
    assert {"path": SURFACE, "source": "local"} in manifest["surfaces"]
    # …but the guard on disk is still v1.
    assert (target / SURFACE / "guard.py").read_text() == GUARD_V1

    # Receipt bump alone is insufficient: doctor still flags the surface.
    findings = doctor(target=target)
    stale = [f for f in findings if f["kind"] == "local_stale"]
    assert [f["path"] for f in stale] == [SURFACE]


def test_incident_recovery_via_repair_adopt_stale_local(
    tmp_path: Path, two_release_remote: str
) -> None:
    target = tmp_path / "consumer"
    _install_v1_then_degrade_to_real_files(target, two_release_remote)
    install(target=target, remote_url=two_release_remote, remote_ref="v2")

    report = repair(target=target, adopt_stale_local=[SURFACE])

    assert any(f["kind"] == "local_stale" for f in report["repaired"])
    link = target / SURFACE
    assert link.is_symlink()
    assert (link / "guard.py").read_text() == GUARD_V2
    backups = list((target / ".workstate" / "backup").glob(f"*/{SURFACE}/guard.py"))
    assert backups and backups[0].read_text() == GUARD_V1
    assert not any(
        f["kind"] in {"local_stale", "local_redundant"} for f in doctor(target=target)
    )


def test_consumer_authored_content_is_override_and_never_replaced(
    tmp_path: Path, two_release_remote: str
) -> None:
    target = tmp_path / "consumer"
    target.mkdir()
    install(target=target, remote_url=two_release_remote, remote_ref="v1")
    link = target / SURFACE
    link.unlink()
    link.mkdir(parents=True)
    (link / "guard.py").write_text(CONSUMER)

    install(target=target, remote_url=two_release_remote, remote_ref="v2")

    findings = doctor(target=target)
    override = [f for f in findings if f["kind"] == "local_override"]
    assert [f["path"] for f in override] == [SURFACE]
    assert override[0]["severity"] == "info"

    # Plain repair and even an explicit adopt request must not touch it.
    repair(target=target)
    repair(target=target, adopt_stale_local=[SURFACE])
    assert (target / SURFACE / "guard.py").read_text() == CONSUMER


def _seed_package_ledger(target: Path) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_kind": "package",
        "package_version": "0.2.1",
        "remote_url": "",
        "remote_ref": "",
        "remote_sha": "",
        "surfaces": [{"path": SURFACE, "source": "local"}],
        "configs": [],
        "mcp_servers": [],
    }
    (target / BOOTSTRAP_MANIFEST_NAME).write_text(json.dumps(payload, indent=2) + "\n")
    (target / ".task-state").mkdir(parents=True, exist_ok=True)
    (target / ".task-state" / "handoff.db").write_bytes(b"")


def test_package_source_fallback_divergent_is_override_redundant_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No clone history in package mode: divergent local content falls back to
    local_override; identical content is still local_redundant."""
    import importlib

    # The package re-exports the install() function under the same name as the
    # submodule, so a plain ``import ... as`` would bind the function.
    install_mod = importlib.import_module("workstate_bootstrap.install")

    payload_root = tmp_path / "pkg-payload"
    (payload_root / SURFACE).mkdir(parents=True)
    (payload_root / SURFACE / "guard.py").write_text(GUARD_V2)
    monkeypatch.setattr(
        install_mod, "_package_source_root", lambda package_root: payload_root
    )

    target = tmp_path / "consumer"
    target.mkdir()
    _seed_package_ledger(target)
    local = target / SURFACE
    local.mkdir(parents=True)

    # Older-release content, but package mode has no history to prove it.
    (local / "guard.py").write_text(GUARD_V1)
    kinds = {
        f["kind"]: f
        for f in doctor(target=target)
        if f["kind"].startswith("local_")
    }
    assert set(kinds) == {"local_override"}
    assert kinds["local_override"]["severity"] == "info"

    # Identical content is still detected without history.
    (local / "guard.py").write_text(GUARD_V2)
    kinds = {
        f["kind"]: f
        for f in doctor(target=target)
        if f["kind"].startswith("local_")
    }
    assert set(kinds) == {"local_redundant"}
