"""Doctor classification of ``source=local`` surfaces (implementation note implementation note).

A surface recorded with ``source=local`` was previously excluded from drift
comparison entirely, so a stale bootstrap-era copy of a managed surface was
indistinguishable from a deliberate consumer override and silently starved
updates. Doctor now diffs local content against the clone payload:

- identical to current payload -> ``local_redundant`` finding
- identical to an *older* payload revision (clone git history) ->
  ``local_stale`` finding (the update-starvation case)
- matches no payload revision -> ``local_override``, informational only
  (``severity=info``): respected, listed once, never repair-eligible.

Tests use a seeded-ledger fixture with a real git clone at
``.workstate/remote`` (legacy hoisted layout: surface files live directly at
``<clone>/<surface>``) so history matching exercises real git plumbing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from workstate_bootstrap.install import BOOTSTRAP_MANIFEST_NAME, SCHEMA_VERSION
from workstate_bootstrap.subcommands import doctor, repair


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
    """Real clone at .workstate/remote: rev A ships GUARD_V1, HEAD ships GUARD_V2."""
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


def _seed_ledger(
    target: Path,
    *,
    source: str = "local",
    surfaces: list[dict[str, str]] | None = None,
) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "remote_url": "file:///tmp/fake.git",
        "remote_ref": "v0.1.23",
        "remote_sha": "0" * 40,
        "surfaces": (
            surfaces if surfaces is not None else [{"path": SURFACE, "source": source}]
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


_LOCAL_KINDS = {
    "local_stale",
    "local_redundant",
    "local_override",
    "local_missing",
    "local_unreadable",
}


def _local_kinds(findings: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {f["kind"]: f for f in findings if f["kind"] in _LOCAL_KINDS}


# --- carved-surface fixtures (review finding WS-DOCTOR-LOCAL-01-REVA-001) ---

CARVED = "Makefile.d"  # SURFACE_CHILD_EXCLUSIONS carve: evals.mk never ships
TOOLS_V1 = "# tools v1\n"
TOOLS_V2 = "# tools v2\n"
LIFECYCLE_V1 = "# lifecycle v1\n"
LIFECYCLE_V2 = "# lifecycle v2\n"
EVALS_FRAGMENT = "# evals fragment: carve-excluded private tooling\n"


def _seed_clone_with_carved_history(target: Path) -> Path:
    """Clone whose carved surface evolves: rev A ships v1, HEAD ships v2."""
    clone = target / ".workstate" / "remote"
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
    return clone


def test_doctor_flags_local_surface_matching_older_payload_as_stale(
    tmp_path: Path,
) -> None:
    """The 2026-06-04 incident: real-file copy of rev A while HEAD is rev B."""
    _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path)
    _materialize_local(tmp_path, GUARD_V1)

    kinds = _local_kinds(doctor(target=tmp_path))

    assert set(kinds) == {"local_stale"}
    assert kinds["local_stale"]["path"] == SURFACE


def test_doctor_flags_local_surface_identical_to_payload_as_redundant(
    tmp_path: Path,
) -> None:
    _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path)
    _materialize_local(tmp_path, GUARD_V2)

    kinds = _local_kinds(doctor(target=tmp_path))

    assert set(kinds) == {"local_redundant"}
    assert kinds["local_redundant"]["path"] == SURFACE


def test_doctor_marks_consumer_authored_local_surface_informational(
    tmp_path: Path,
) -> None:
    """Content matching no payload revision is an override: info, not finding."""
    _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path)
    _materialize_local(tmp_path, CONSUMER)

    kinds = _local_kinds(doctor(target=tmp_path))

    assert set(kinds) == {"local_override"}
    assert kinds["local_override"]["severity"] == "info"


def test_doctor_local_classification_skips_shared_surfaces(tmp_path: Path) -> None:
    """source=shared keeps the existing surface_drift path; no local_* kinds."""
    _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path, source="shared")
    _materialize_local(tmp_path, GUARD_V1)  # real dir where symlink expected

    findings = doctor(target=tmp_path)

    assert not _local_kinds(findings)
    assert any(f["kind"] == "surface_drift" for f in findings)


def test_cli_doctor_exits_one_for_local_stale(tmp_path: Path, capsys) -> None:
    from workstate_bootstrap.cli import main

    _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path)
    _materialize_local(tmp_path, GUARD_V1)

    rc = main(["doctor", "--target", str(tmp_path)])

    assert rc == 1
    assert f"local_stale: {SURFACE}" in capsys.readouterr().out


def test_cli_doctor_override_note_does_not_affect_exit_code(
    tmp_path: Path, capsys
) -> None:
    from workstate_bootstrap.cli import main

    _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path)
    _materialize_local(tmp_path, CONSUMER)

    rc = main(["doctor", "--target", str(tmp_path)])

    out = capsys.readouterr().out
    assert f"note local_override: {SURFACE}" in out
    assert rc == 0, out


def test_repair_never_touches_local_override_and_defers_local_stale(
    tmp_path: Path,
) -> None:
    """Until the opt-in adoption path (implementation note) lands, repair must leave local
    surfaces alone: stale -> skipped, override -> not even listed."""
    _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path)
    _materialize_local(tmp_path, GUARD_V1)

    report = repair(target=tmp_path)

    assert (tmp_path / SURFACE / "guard.py").read_text() == GUARD_V1
    assert all(f["kind"] != "local_override" for f in report["repaired"])
    assert all(f["kind"] != "local_override" for f in report["skipped"])
    assert all(f["kind"] != "local_stale" for f in report["repaired"])


def test_cli_doctor_exits_one_for_local_redundant(tmp_path: Path, capsys) -> None:
    from workstate_bootstrap.cli import main

    _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path)
    _materialize_local(tmp_path, GUARD_V2)

    rc = main(["doctor", "--target", str(tmp_path)])

    assert rc == 1
    assert f"local_redundant: {SURFACE}" in capsys.readouterr().out


def test_doctor_classifies_carved_surface_stale_despite_excluded_children(
    tmp_path: Path,
) -> None:
    """A full bootstrap-era copy of a carved surface (excluded child included)
    must match the older payload revision minus the carve — not fall through
    to local_override because the payload tree still ships evals.mk."""
    _seed_clone_with_carved_history(tmp_path)
    _seed_ledger(tmp_path, surfaces=[{"path": CARVED, "source": "local"}])
    local = tmp_path / CARVED
    local.mkdir(parents=True)
    (local / "tools.mk").write_text(TOOLS_V1)
    (local / "lifecycle.mk").write_text(LIFECYCLE_V1)
    (local / "evals.mk").write_text(EVALS_FRAGMENT)

    kinds = _local_kinds(doctor(target=tmp_path))

    assert set(kinds) == {"local_stale"}
    assert kinds["local_stale"]["path"] == CARVED


def test_doctor_classifies_carved_surface_redundant_minus_excluded_children(
    tmp_path: Path,
) -> None:
    """Current-content carved copy is redundant even when the local tree
    carries junk under a carve-excluded name; excluded children never
    participate in classification on either side."""
    _seed_clone_with_carved_history(tmp_path)
    _seed_ledger(tmp_path, surfaces=[{"path": CARVED, "source": "local"}])
    local = tmp_path / CARVED
    local.mkdir(parents=True)
    (local / "tools.mk").write_text(TOOLS_V2)
    (local / "lifecycle.mk").write_text(LIFECYCLE_V2)
    (local / "evals.mk").write_text("# consumer junk in a carved-out name\n")

    kinds = _local_kinds(doctor(target=tmp_path))

    assert set(kinds) == {"local_redundant"}
    assert kinds["local_redundant"]["path"] == CARVED


def test_doctor_names_broken_local_surface_instead_of_silent_skip(
    tmp_path: Path, capsys
) -> None:
    """A source=local surface that is a broken symlink must surface as an
    informational local_missing note (never-silent), not vanish from the
    report; info severity keeps the exit code at 0."""
    from workstate_bootstrap.cli import main

    _seed_clone_with_history(tmp_path)
    _seed_ledger(tmp_path)
    (tmp_path / SURFACE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / SURFACE).symlink_to(tmp_path / "nonexistent-target")

    kinds = _local_kinds(doctor(target=tmp_path))
    assert "local_missing" in kinds
    assert kinds["local_missing"]["severity"] == "info"

    rc = main(["doctor", "--target", str(tmp_path)])
    out = capsys.readouterr().out
    assert f"note local_missing: {SURFACE}" in out
    assert rc == 0, out


def test_doctor_mixed_local_surfaces_classified_independently(
    tmp_path: Path,
) -> None:
    """One stale copy plus one consumer override in the same run: each gets
    its own classification; neither masks the other."""
    clone = _seed_clone_with_history(tmp_path)
    other = ".github/hooks"
    (clone / other).mkdir(parents=True)
    (clone / other / "hook.py").write_text(GUARD_V1)
    _git(clone, "add", "-A")
    _git(clone, "commit", "--quiet", "-m", "rev C: add second surface")
    _seed_ledger(
        tmp_path,
        surfaces=[
            {"path": SURFACE, "source": "local"},
            {"path": other, "source": "local"},
        ],
    )
    _materialize_local(tmp_path, GUARD_V1)
    (tmp_path / other).mkdir(parents=True)
    (tmp_path / other / "hook.py").write_text(CONSUMER)

    findings = doctor(target=tmp_path)
    by_path = {f["path"]: f["kind"] for f in findings if f["kind"] in _LOCAL_KINDS}

    assert by_path[SURFACE] == "local_stale"
    assert by_path[other] == "local_override"
