"""implementation note contract tests for the lifecycle ``doctor`` subcommand (implementation note).

The handler is the cold-start aggregator described in
``docs/plans/0010-frictionless-receipts-and-deferred-cleanup.md`` implementation note.
It composes facets from the existing ``status``-style helpers (env,
mcp, branch, lifecycle, dashboard, hooks) plus a ``next_command`` hint
and emits a ``DoctorReceipt`` JSON line so first-turn agents stop
spelunking through ``make context`` + ``DASHBOARD.txt`` + raw MCP reads.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "scripts" / "workstate" / "lifecycle"
CONTRACT_SOURCE = (
    PACKAGE_ROOT / "docs" / "workstate" / "contracts" / "harness-protocol.yaml"
)


def _seed_contract(repo: Path) -> None:
    """Copy harness-protocol.yaml so the branch-isolation policy loader
    can resolve protected paths inside the fixture repo."""
    target = repo / "docs" / "workstate" / "contracts" / "harness-protocol.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CONTRACT_SOURCE, target)


def _seed_hook_helpers(repo: Path) -> None:
    """Mirror the dirty-main probe's hook-helper imports into the fixture
    repo so the doctor facet exercises the real probe path rather than
    the no-policy fallback."""
    src = PACKAGE_ROOT / "scripts" / "hooks"
    dest = repo / "packages" / "workstate-system" / "scripts" / "hooks"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("_branch_isolation_guard.py", "_harness_protocol.py"):
        shutil.copy2(src / name, dest / name)


def _run_doctor(
    cwd: Path,
    *extra_argv: str,
    handoff_bin: str | None = "/nonexistent/no-such-binary-xyz",
    orchestrator_bin: str | None = "/nonexistent/no-such-binary-xyz",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if handoff_bin is not None:
        env["MCP_WORKSTATE_HANDOFF_BIN"] = handoff_bin
    # WORKSTATE-REF-06 implementation note: short-circuit the new orchestrator probe the same
    # way `handoff_bin` short-circuits the handoff probe, so subprocess
    # tests don't pay the full retry budget on an unmocked daemon.
    if orchestrator_bin is not None:
        env["MCP_WORKSTATE_ORCHESTRATOR_BIN"] = orchestrator_bin
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "doctor", *extra_argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text("doctor:\n\t@true\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "-C", str(repo), "add", "Makefile"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        check=True,
    )
    return repo


def test_doctor_emits_receipt_with_expected_top_level_keys(git_repo: Path) -> None:
    """JSON receipt must publish the implementation note §Resolved invocation strategy facets."""
    proc = _run_doctor(git_repo, "--json")

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["command"] == "doctor"
    for facet in (
        "env",
        "mcp",
        "branch",
        "lifecycle",
        "dashboard",
        "hooks",
        "dirty_main",
    ):
        assert facet in receipt, f"missing facet {facet!r}: {sorted(receipt.keys())}"
    assert "next_command" in receipt
    assert "warnings" in receipt and isinstance(receipt["warnings"], list)


# WORKSTATE-REF-53 implementation note: dirty-main doctor facet exposes ownership-aware
# diagnostics. On a clean root `main`, the facet still renders so callers
# can rely on a stable shape; protected_paths_dirty must be empty.
def test_doctor_dirty_main_facet_clean_main(git_repo: Path) -> None:
    proc = _run_doctor(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    dirty_main = receipt["dirty_main"]
    assert dirty_main["branch"] in ("main", "master"), (
        "WORKSTATE-REF-53 implementation note: dirty_main facet must report the active branch "
        f"so operators can correlate the report; got {dirty_main!r}"
    )
    assert dirty_main["protected_paths_dirty"] == [], (
        "WORKSTATE-REF-53 implementation note: clean main must report no dirty protected paths; "
        f"got {dirty_main['protected_paths_dirty']!r}"
    )
    assert dirty_main["mode_recommended"] == "warn", (
        "WORKSTATE-REF-53 implementation note: clean main recommends `warn` mode (no action needed); "
        f"got {dirty_main['mode_recommended']!r}"
    )
    assert isinstance(dirty_main["remediation"], list)
    assert dirty_main["ownership_hint"] is None, (
        "WORKSTATE-REF-53 implementation note: ownership_hint must be null when no dirty paths exist; "
        f"got {dirty_main['ownership_hint']!r}"
    )


def test_doctor_dirty_main_facet_dirty_protected_path_recommends_doctor_mode(
    git_repo: Path,
) -> None:
    """When a protected path is dirty on root main, the facet should escalate.

    The recommended mode shifts from `warn` (routine, no findings) to
    `doctor` (operator should triage). Remediation guidance must be
    non-empty so the operator knows the next safe action without
    grepping the hook source.
    """
    # Create a dirty file under a protected path. The contract YAML
    # protects ``packages/`` and similar core source roots.
    target = (
        git_repo / "packages" / "workstate-system" / "scripts" / "workstate" / "marker.txt"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("dirty-marker\n")

    proc = _run_doctor(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    dirty_main = receipt["dirty_main"]

    # When the harness-protocol contract is not present in the fixture
    # repo, the helper falls back gracefully — we only require the facet
    # to exist with a stable shape, not specific path detection.
    assert "protected_paths_dirty" in dirty_main
    assert "mode_recommended" in dirty_main
    assert dirty_main["mode_recommended"] in {"warn", "doctor", "block"}
    assert isinstance(dirty_main["remediation"], list)


def test_doctor_dirty_main_facet_with_policy_present_names_dirty_path(
    tmp_path: Path,
) -> None:
    """WORKSTATE-REF-53-S3-BR-01: with the harness-protocol contract seeded so the
    branch-isolation policy loader resolves protected paths, a dirty file
    under a protected root on main must surface in
    ``protected_paths_dirty`` and escalate ``mode_recommended`` to
    ``doctor``. Without this guard the only existing dirty-path test
    accepts the no-policy fallback (``warn``) and leaves the doctor-mode
    branch unverified — which is exactly what implementation note review flagged.
    """
    repo = tmp_path / "primary"
    repo.mkdir()
    (repo / "Makefile").write_text("doctor:\n\t@true\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    _seed_contract(repo)
    _seed_hook_helpers(repo)
    guarded = repo / "packages" / "workstate-system" / "scripts" / "guarded.py"
    guarded.parent.mkdir(parents=True, exist_ok=True)
    guarded.write_text("# baseline\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        check=True,
    )
    # Dirty the protected path without committing.
    guarded.write_text("# dirty\n")

    proc = _run_doctor(repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    dirty_main = receipt["dirty_main"]

    assert dirty_main["protected_paths_dirty"], (
        "WORKSTATE-REF-53-S3-BR-01: with policy present and a dirty protected path, "
        f"protected_paths_dirty must be non-empty; got {dirty_main!r}"
    )
    assert any(
        "guarded.py" in entry for entry in dirty_main["protected_paths_dirty"]
    ), (
        "WORKSTATE-REF-53-S3-BR-01: protected_paths_dirty must name the dirty file; "
        f"got {dirty_main['protected_paths_dirty']!r}"
    )
    assert dirty_main["mode_recommended"] == "doctor", (
        "WORKSTATE-REF-53-S3-BR-01: dirty protected path on main must escalate "
        f"mode_recommended to 'doctor'; got {dirty_main['mode_recommended']!r}"
    )
    assert dirty_main["remediation"], (
        "WORKSTATE-REF-53-S3-BR-01: remediation guidance must be non-empty when the "
        "facet escalates so operators have a next safe action."
    )


# WORKSTATE-REF-72 implementation note: plan_baseline facet aggregates across live handoff rows.


def _write_doctor_baseline_cli(target: Path, rows: list[dict]) -> None:
    """Write a fake CLI that answers ``handoff-rows`` with ``rows`` and a
    minimal ``review-runs`` / ``review-findings`` stub. The evaluator
    falls back to ``unknown`` when it cannot reach MCP; we only need the
    handoff-rows branch to return a parseable list for the doctor probe.
    """
    import json as _json
    import stat as _stat

    rows_json = _json.dumps(rows)
    body = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "argv = sys.argv[1:]\n"
        "if 'handoff-rows' in argv:\n"
        f"    print({rows_json!r})\n"
        "elif 'review-runs' in argv:\n"
        "    print(json.dumps({'ok': True, 'data': {'runs': []}}))\n"
        "elif 'review-findings' in argv:\n"
        "    print(json.dumps({'ok': True, 'data': {'counts': {'status': {'open': 0}}}}))\n"
        "elif 'state' in argv:\n"
        "    print(json.dumps({'ok': True, 'data': {'active': {}}}))\n"
        "else:\n"
        "    print(json.dumps({'ok': False}))\n"
    )
    target.write_text(body)
    target.chmod(target.stat().st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)


def test_doctor_plan_baseline_facet_aggregates_live_rows(
    git_repo: Path, tmp_path: Path
) -> None:
    """The doctor receipt exposes a ``plan_baseline`` facet whose counts
    aggregate baseline state across every live handoff row. With MCP
    reachable, ``available=True`` and counts sum to the row total."""
    # Commit an accepted plan on main so one row resolves to "accepted".
    plan_path = git_repo / "plans" / "WORKSTATE-REF-77.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# WORKSTATE-REF-77 Plan\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "plans/WORKSTATE-REF-77.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(git_repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-m",
            "accept plan",
            "-q",
        ],
        check=True,
    )

    fake_cli = tmp_path / "mcp-workstate-handoff"
    _write_doctor_baseline_cli(
        fake_cli,
        rows=[
            {
                "task_ref": "WORKSTATE-REF-77",
                "target_branch": "feature/WORKSTATE-77-x",
                "task_plan_path": "plans/WORKSTATE-REF-77.md",
            },
            {
                "task_ref": "WORKSTATE-REF-88",
                "target_branch": "feature/WORKSTATE-88-y",
                "task_plan_path": "plans/WORKSTATE-REF-88.md",
            },
        ],
    )

    proc = _run_doctor(git_repo, "--json", handoff_bin=str(fake_cli))
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    facet = receipt.get("plan_baseline")
    assert facet is not None
    assert facet["available"] is True
    counts = facet["counts"]
    assert counts.get("accepted", 0) == 1
    assert counts.get("missing", 0) == 1
    assert len(facet["baselines"]) == 2


def test_doctor_plan_baseline_facet_unavailable_when_mcp_unreachable(
    git_repo: Path,
) -> None:
    """When MCP cannot list handoff rows the doctor facet must collapse to
    ``available=False`` so callers render ``baseline=unknown`` rather than
    silently passing."""
    proc = _run_doctor(git_repo, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    facet = receipt["plan_baseline"]
    assert facet["available"] is False
    assert facet["counts"] == {}
    assert facet["baselines"] == []


def test_doctor_rejects_unknown_output_format_at_parse_time(git_repo: Path) -> None:
    """Invalid suffix args must be rejected at argparse time, not at handler dispatch."""
    proc = _run_doctor(git_repo, "--json", "--output-format", "yaml")

    assert proc.returncode != 0
    assert "yaml" in proc.stderr or "output-format" in proc.stderr


# ---------------------------------------------------------------------------
# WORKSTATE-REF-06 implementation note: tri-state DoctorMcp + bounded retry on the handoff probe.
#
# The pre-WORKSTATE-REF-06 code at handlers/doctor.py:132-155 runs a single 0.5s probe
# against `mcp-workstate-handoff state`. On a cold session the binary's first
# Python import + SQLite open + FTS5 attach can exceed that budget, so the
# probe loses, `handoff_reachable=False` lands in the receipt, and
# `_suggest_next` tells the operator to "restart workstate-handoff-mcp" — exactly
# wrong for a server that just needs a few hundred more milliseconds. These
# tests pin (a) the tri-state field shape, (b) bounded fail-then-succeed
# retry (warming), (c) bounded retry exhaustion (unreachable), and (d) the
# missing-binary short-circuit so retries don't burn wall-clock on a path
# that will never resolve.
# ---------------------------------------------------------------------------


_LIFECYCLE_PKG_DIR = PACKAGE_ROOT / "scripts" / "workstate" / "lifecycle"
if str(_LIFECYCLE_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_LIFECYCLE_PKG_DIR))


def _probe_mcp_via_handler(repo: Path):
    """Import the handler lazily so the sys.path tweak above is honored."""
    from workstate.lifecycle.handlers import doctor as doctor_handler  # noqa: PLC0415

    return doctor_handler, doctor_handler._probe_mcp(repo)


def _stub_orchestrator_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the WORKSTATE-REF-06 implementation note orchestrator probe to a fast unreachable.

    implementation note tests focus on the handoff probe shape; they should not pay the
    cost of an unmocked subprocess against ``mcp-workstate-orchestrator`` and
    should not have to care whether the orchestrator probe is wired into
    ``_probe_mcp`` (implementation note) or runs independently (implementation note / later). Just
    pin orchestrator_status="unreachable" without touching its semantics.
    """
    from workstate.lifecycle.handlers import _common as common_mod  # noqa: PLC0415
    from receipts import ReceiptWarning  # noqa: PLC0415

    def _fake_orchestrator(repo, *, argv, timeout_seconds, field):
        return None, ReceiptWarning(field=field, reason="unavailable")

    if hasattr(common_mod, "run_orchestrator_json"):
        monkeypatch.setattr(common_mod, "run_orchestrator_json", _fake_orchestrator)


def test_WORKSTATE06_doctor_mcp_tri_state_reachable_on_first_attempt(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-attempt success surfaces `mcp_status="reachable"` and keeps
    `handoff_reachable=True` as a derived back-compat view."""
    from workstate.lifecycle.handlers import _common as common_mod  # noqa: PLC0415

    def _fake_probe(repo, *, argv, timeout_seconds, field):
        return ({"ok": True, "data": {"active": {}}}, None)

    monkeypatch.setattr(common_mod, "run_handoff_json", _fake_probe)
    _stub_orchestrator_unreachable(monkeypatch)
    # Pre-flight resolution must not reject the binary in this test:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/true")

    doctor_handler, (mcp, warnings) = _probe_mcp_via_handler(git_repo)

    assert mcp.mcp_status == "reachable", (
        f"WORKSTATE-REF-06 implementation note: first-attempt success must surface "
        f"mcp_status='reachable'; got {mcp.mcp_status!r}"
    )
    assert mcp.handoff_reachable is True, (
        "WORKSTATE-REF-06 implementation note: handoff_reachable must remain True for back-compat "
        "when mcp_status='reachable'."
    )


def test_WORKSTATE06_doctor_mcp_tri_state_warming_after_one_retry(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that fails once then succeeds surfaces `mcp_status="warming"`
    so `_suggest_next` can distinguish cold-start from outage. The legacy
    `handoff_reachable` boolean stays True because the probe ultimately
    succeeded."""
    from workstate.lifecycle.handlers import _common as common_mod  # noqa: PLC0415
    from receipts import ReceiptWarning  # noqa: PLC0415

    attempts = {"count": 0}

    def _fake_probe(repo, *, argv, timeout_seconds, field):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return None, ReceiptWarning(
                field=field, reason="timeout", exception_type="TimeoutExpired"
            )
        return ({"ok": True, "data": {"active": {}}}, None)

    monkeypatch.setattr(common_mod, "run_handoff_json", _fake_probe)
    _stub_orchestrator_unreachable(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/true")
    # Keep the retry sleeps from making the test slow.
    monkeypatch.setattr("time.sleep", lambda _s: None)

    _, (mcp, warnings) = _probe_mcp_via_handler(git_repo)

    assert attempts["count"] >= 2, (
        "WORKSTATE-REF-06 implementation note: probe must retry after the first attempt fails; "
        f"got attempts={attempts['count']}"
    )
    assert mcp.mcp_status == "warming", (
        "WORKSTATE-REF-06 implementation note: probe success on retry must surface "
        f"mcp_status='warming'; got {mcp.mcp_status!r}"
    )
    assert mcp.handoff_reachable is True, (
        "WORKSTATE-REF-06 implementation note: handoff_reachable derived view must be True when "
        "mcp_status='warming' (the probe did ultimately succeed)."
    )


def test_WORKSTATE06_doctor_mcp_tri_state_unreachable_after_budget(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that fails every attempt within the retry budget surfaces
    `mcp_status="unreachable"`. The number of attempts must be bounded
    (no infinite retry) and `handoff_reachable` follows the derived
    view."""
    from workstate.lifecycle.handlers import _common as common_mod  # noqa: PLC0415
    from receipts import ReceiptWarning  # noqa: PLC0415

    attempts = {"count": 0}

    def _fake_probe(repo, *, argv, timeout_seconds, field):
        attempts["count"] += 1
        return None, ReceiptWarning(
            field=field, reason="timeout", exception_type="TimeoutExpired"
        )

    monkeypatch.setattr(common_mod, "run_handoff_json", _fake_probe)
    _stub_orchestrator_unreachable(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/true")
    monkeypatch.setattr("time.sleep", lambda _s: None)

    _, (mcp, warnings) = _probe_mcp_via_handler(git_repo)

    assert 2 <= attempts["count"] <= 8, (
        "WORKSTATE-REF-06 implementation note: probe must retry on failure but stay bounded "
        f"(2..8 attempts); got attempts={attempts['count']}"
    )
    assert mcp.mcp_status == "unreachable", (
        "WORKSTATE-REF-06 implementation note: exhausted retry budget must surface "
        f"mcp_status='unreachable'; got {mcp.mcp_status!r}"
    )
    assert mcp.handoff_reachable is False, (
        "WORKSTATE-REF-06 implementation note: handoff_reachable derived view must be False when "
        "mcp_status='unreachable'."
    )


def test_WORKSTATE06_doctor_mcp_short_circuits_on_missing_binary(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the configured `mcp-workstate-handoff` binary does not resolve, the
    probe must short-circuit to `mcp_status="unreachable"` after a single
    attempt — retrying a non-existent path just burns wall-clock."""
    from workstate.lifecycle.handlers import _common as common_mod  # noqa: PLC0415
    from receipts import ReceiptWarning  # noqa: PLC0415

    attempts = {"count": 0}

    def _fake_probe(repo, *, argv, timeout_seconds, field):
        attempts["count"] += 1
        return None, ReceiptWarning(field=field, reason="unavailable")

    monkeypatch.setattr(common_mod, "run_handoff_json", _fake_probe)
    _stub_orchestrator_unreachable(monkeypatch)
    # shutil.which returns None for an absolute path that does not exist,
    # which is the deterministic short-circuit signal.
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    _, (mcp, warnings) = _probe_mcp_via_handler(git_repo)

    assert attempts["count"] <= 1, (
        "WORKSTATE-REF-06 implementation note: missing binary must short-circuit retries; "
        f"got attempts={attempts['count']}"
    )
    assert mcp.mcp_status == "unreachable", (
        "WORKSTATE-REF-06 implementation note: missing binary must surface "
        f"mcp_status='unreachable'; got {mcp.mcp_status!r}"
    )


# ---------------------------------------------------------------------------
# WORKSTATE-REF-06 implementation note: real orchestrator probe + tri-state `orchestrator_status`.
#
# implementation note left `orchestrator_reachable=False` hardcoded. implementation note replaces
# that with a bounded retry/backoff probe against the orchestrator CLI
# (`mcp-workstate-orchestrator orchestrator-status`) and surfaces the same
# tri-state shape as the handoff probe so `_suggest_next` (implementation note) can
# distinguish cold-start from genuine outage on either endpoint
# independently.
# ---------------------------------------------------------------------------


def _stub_handoff_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the handoff probe to first-attempt success so implementation note tests can
    isolate orchestrator probe behaviour."""
    from workstate.lifecycle.handlers import _common as common_mod  # noqa: PLC0415

    def _fake_handoff(repo, *, argv, timeout_seconds, field):
        return ({"ok": True, "data": {"active": {}}}, None)

    monkeypatch.setattr(common_mod, "run_handoff_json", _fake_handoff)


def test_WORKSTATE06_doctor_orchestrator_tri_state_reachable_on_first_attempt(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-attempt orchestrator success surfaces
    `orchestrator_status="reachable"` and keeps `orchestrator_reachable=True`
    as a derived back-compat view."""
    from workstate.lifecycle.handlers import _common as common_mod  # noqa: PLC0415

    _stub_handoff_reachable(monkeypatch)

    def _fake_orchestrator(repo, *, argv, timeout_seconds, field):
        return ({"ok": True, "status": "running"}, None)

    monkeypatch.setattr(common_mod, "run_orchestrator_json", _fake_orchestrator)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/true")

    _, (mcp, warnings) = _probe_mcp_via_handler(git_repo)

    assert mcp.orchestrator_status == "reachable", (
        f"WORKSTATE-REF-06 implementation note: first-attempt orchestrator success must surface "
        f"orchestrator_status='reachable'; got {mcp.orchestrator_status!r}"
    )
    assert mcp.orchestrator_reachable is True, (
        "WORKSTATE-REF-06 implementation note: orchestrator_reachable must be True when "
        "orchestrator_status='reachable'."
    )


def test_WORKSTATE06_doctor_orchestrator_tri_state_warming_after_one_retry(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-then-succeed orchestrator probe surfaces
    `orchestrator_status="warming"`; derived `orchestrator_reachable` stays
    True because the probe ultimately succeeded."""
    from workstate.lifecycle.handlers import _common as common_mod  # noqa: PLC0415
    from receipts import ReceiptWarning  # noqa: PLC0415

    _stub_handoff_reachable(monkeypatch)
    attempts = {"count": 0}

    def _fake_orchestrator(repo, *, argv, timeout_seconds, field):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return None, ReceiptWarning(
                field=field, reason="timeout", exception_type="TimeoutExpired"
            )
        return ({"ok": True, "status": "running"}, None)

    monkeypatch.setattr(common_mod, "run_orchestrator_json", _fake_orchestrator)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/true")
    monkeypatch.setattr("time.sleep", lambda _s: None)

    _, (mcp, warnings) = _probe_mcp_via_handler(git_repo)

    assert attempts["count"] >= 2, (
        "WORKSTATE-REF-06 implementation note: orchestrator probe must retry after the first "
        f"attempt fails; got attempts={attempts['count']}"
    )
    assert mcp.orchestrator_status == "warming", (
        "WORKSTATE-REF-06 implementation note: orchestrator success on retry must surface "
        f"orchestrator_status='warming'; got {mcp.orchestrator_status!r}"
    )
    assert mcp.orchestrator_reachable is True, (
        "WORKSTATE-REF-06 implementation note: orchestrator_reachable derived view must be True "
        "when orchestrator_status='warming'."
    )


def test_WORKSTATE06_doctor_orchestrator_tri_state_unreachable_after_budget(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Orchestrator probe that fails every attempt within the retry budget
    surfaces `orchestrator_status="unreachable"`. Attempts must be bounded
    (no infinite retry)."""
    from workstate.lifecycle.handlers import _common as common_mod  # noqa: PLC0415
    from receipts import ReceiptWarning  # noqa: PLC0415

    _stub_handoff_reachable(monkeypatch)
    attempts = {"count": 0}

    def _fake_orchestrator(repo, *, argv, timeout_seconds, field):
        attempts["count"] += 1
        return None, ReceiptWarning(
            field=field, reason="timeout", exception_type="TimeoutExpired"
        )

    monkeypatch.setattr(common_mod, "run_orchestrator_json", _fake_orchestrator)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/true")
    monkeypatch.setattr("time.sleep", lambda _s: None)

    _, (mcp, warnings) = _probe_mcp_via_handler(git_repo)

    assert 2 <= attempts["count"] <= 8, (
        "WORKSTATE-REF-06 implementation note: orchestrator probe must retry on failure but stay "
        f"bounded (2..8 attempts); got attempts={attempts['count']}"
    )
    assert mcp.orchestrator_status == "unreachable", (
        "WORKSTATE-REF-06 implementation note: exhausted orchestrator retry budget must surface "
        f"orchestrator_status='unreachable'; got {mcp.orchestrator_status!r}"
    )
    assert mcp.orchestrator_reachable is False, (
        "WORKSTATE-REF-06 implementation note: orchestrator_reachable derived view must be False "
        "when orchestrator_status='unreachable'."
    )


def test_WORKSTATE06_doctor_orchestrator_short_circuits_on_missing_binary(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the configured `mcp-workstate-orchestrator` binary does not
    resolve, the orchestrator probe must short-circuit to
    `orchestrator_status="unreachable"` after a single attempt."""
    from workstate.lifecycle.handlers import _common as common_mod  # noqa: PLC0415
    from receipts import ReceiptWarning  # noqa: PLC0415

    _stub_handoff_reachable(monkeypatch)
    attempts = {"count": 0}

    def _fake_orchestrator(repo, *, argv, timeout_seconds, field):
        attempts["count"] += 1
        return None, ReceiptWarning(field=field, reason="unavailable")

    monkeypatch.setattr(common_mod, "run_orchestrator_json", _fake_orchestrator)
    # shutil.which returns None for both binaries; the orchestrator probe
    # should short-circuit on the missing orchestrator binary regardless of
    # whether the handoff probe was independently mocked above.
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    _, (mcp, warnings) = _probe_mcp_via_handler(git_repo)

    assert attempts["count"] <= 1, (
        "WORKSTATE-REF-06 implementation note: missing orchestrator binary must short-circuit "
        f"retries; got attempts={attempts['count']}"
    )
    assert mcp.orchestrator_status == "unreachable", (
        "WORKSTATE-REF-06 implementation note: missing orchestrator binary must surface "
        f"orchestrator_status='unreachable'; got {mcp.orchestrator_status!r}"
    )


def test_WORKSTATE06_doctor_orchestrator_latency_recorded(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """implementation note records per-attempt orchestrator latency under
    ``latencies_ms["orchestrator"]`` so the receipt carries both handoff
    and orchestrator timings."""
    from workstate.lifecycle.handlers import _common as common_mod  # noqa: PLC0415

    _stub_handoff_reachable(monkeypatch)

    def _fake_orchestrator(repo, *, argv, timeout_seconds, field):
        return ({"ok": True, "status": "running"}, None)

    monkeypatch.setattr(common_mod, "run_orchestrator_json", _fake_orchestrator)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/true")

    _, (mcp, _warnings) = _probe_mcp_via_handler(git_repo)

    assert "orchestrator" in mcp.latencies_ms, (
        "WORKSTATE-REF-06 implementation note: orchestrator probe must record a latency under "
        f"latencies_ms['orchestrator']; got keys={list(mcp.latencies_ms)!r}"
    )
    assert mcp.latencies_ms["orchestrator"] >= 0.0


# ---------------------------------------------------------------------------
# WORKSTATE-REF-06 implementation note: `_suggest_next` + stderr summary line rewired through the
# tri-state so cold-start (`warming`) and outage (`unreachable`) produce
# distinct operator remediations on either endpoint.
# ---------------------------------------------------------------------------


def test_WORKSTATE06_suggest_next_recommends_wait_when_handoff_warming(
    git_repo: Path,
) -> None:
    """`mcp.mcp_status="warming"` must produce a "wait and retry" next-command
    so the operator does not restart a healthy-but-cold server."""
    from workstate.lifecycle.handlers import doctor as doctor_handler  # noqa: PLC0415
    from receipts import DoctorBranch, DoctorMcp  # noqa: PLC0415

    branch = DoctorBranch(name="main", head="abcdef", ahead_of_main=None, dirty=0)
    mcp = DoctorMcp(
        handoff_reachable=True,
        orchestrator_reachable=True,
        latencies_ms={"handoff": 0.0, "orchestrator": 0.0},
        mcp_status="warming",
        orchestrator_status="reachable",
    )
    nxt = doctor_handler._suggest_next(branch, mcp)
    assert "wait" in nxt.command.lower() and (
        "doctor" in nxt.command.lower() or "retry" in nxt.command.lower()
    ), (
        "WORKSTATE-REF-06 implementation note: warming handoff must recommend wait-and-retry; "
        f"got next_command={nxt.command!r}"
    )
    assert "warming" in nxt.reason.lower() or "cold" in nxt.reason.lower(), (
        "WORKSTATE-REF-06 implementation note: warming-state next_command.reason must explain the "
        f"cold-start situation; got reason={nxt.reason!r}"
    )


def test_WORKSTATE06_suggest_next_preserves_unreachable_remediation(
    git_repo: Path,
) -> None:
    """`mcp.mcp_status="unreachable"` must keep the existing restart
    remediation so genuine outage still produces the operator-actionable
    string."""
    from workstate.lifecycle.handlers import doctor as doctor_handler  # noqa: PLC0415
    from receipts import DoctorBranch, DoctorMcp  # noqa: PLC0415

    branch = DoctorBranch(name="main", head="abcdef", ahead_of_main=None, dirty=0)
    mcp = DoctorMcp(
        handoff_reachable=False,
        orchestrator_reachable=False,
        latencies_ms={"handoff": 0.0, "orchestrator": 0.0},
        mcp_status="unreachable",
        orchestrator_status="unreachable",
    )
    nxt = doctor_handler._suggest_next(branch, mcp)
    assert (
        "MCP_WORKSTATE_HANDOFF_BIN" in nxt.command and "restart" in nxt.command.lower()
    ), (
        "WORKSTATE-REF-06 implementation note: unreachable handoff must preserve the "
        "`check MCP_WORKSTATE_HANDOFF_BIN; restart workstate-handoff-mcp` remediation; "
        f"got next_command={nxt.command!r}"
    )


def test_WORKSTATE06_suggest_next_flags_orchestrator_warming_when_handoff_ok(
    git_repo: Path,
) -> None:
    """When only the orchestrator is warming, the next_command should still
    surface that state so the operator does not erroneously think both
    daemons are degraded."""
    from workstate.lifecycle.handlers import doctor as doctor_handler  # noqa: PLC0415
    from receipts import DoctorBranch, DoctorMcp  # noqa: PLC0415

    branch = DoctorBranch(
        name="feature/test", head="abcdef", ahead_of_main=None, dirty=0
    )
    mcp = DoctorMcp(
        handoff_reachable=True,
        orchestrator_reachable=True,
        latencies_ms={"handoff": 0.0, "orchestrator": 0.0},
        mcp_status="reachable",
        orchestrator_status="warming",
    )
    nxt = doctor_handler._suggest_next(branch, mcp)
    # The reason or command must mention orchestrator warming explicitly so
    # the operator can correlate the recommendation to the right endpoint.
    combined = f"{nxt.command} {nxt.reason}".lower()
    assert "orchestrator" in combined and ("warm" in combined or "wait" in combined), (
        "WORKSTATE-REF-06 implementation note: orchestrator warming must be reflected in the "
        f"next_command surface; got command={nxt.command!r} reason={nxt.reason!r}"
    )


def test_WORKSTATE06_summary_line_includes_mcp_and_orchestrator_tri_state(
    git_repo: Path,
) -> None:
    """The human-readable stderr summary must publish both `mcp=<status>`
    and `orchestrator=<status>` tokens so the operator can read tri-states
    without parsing JSON."""
    proc = _run_doctor(git_repo)  # human-readable mode (no --json)
    assert proc.returncode == 0, proc.stderr
    stderr = proc.stderr
    assert "mcp=unreachable" in stderr, (
        "WORKSTATE-REF-06 implementation note: summary line must include `mcp=<status>` token; "
        f"got stderr={stderr!r}"
    )
    assert "orchestrator=unreachable" in stderr, (
        "WORKSTATE-REF-06 implementation note: summary line must include `orchestrator=<status>` "
        f"token; got stderr={stderr!r}"
    )


# ---------------------------------------------------------------------------
# WORKSTATE-REF-06 implementation note: regression coverage pin. These three tests are named and
# scoped to the WORKSTATE-REF-06 epic deliverable bullets so a future refactor cannot
# silently regress cold-start behaviour without explicitly deleting a test
# whose docstring names the regression it guards against.
# ---------------------------------------------------------------------------


def test_doctor_probe_cold_start_warming_then_reachable(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKSTATE-REF-06 deliverable: cold-start surfaces ``warming`` instead of
    ``unreachable`` when the handoff probe succeeds on retry."""
    from workstate.lifecycle.handlers import _common as common_mod  # noqa: PLC0415
    from receipts import ReceiptWarning  # noqa: PLC0415

    attempts = {"count": 0}

    def _fake_handoff(repo, *, argv, timeout_seconds, field):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return None, ReceiptWarning(
                field=field, reason="timeout", exception_type="TimeoutExpired"
            )
        return ({"ok": True, "data": {"active": {}}}, None)

    monkeypatch.setattr(common_mod, "run_handoff_json", _fake_handoff)
    _stub_orchestrator_unreachable(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/true")
    monkeypatch.setattr("time.sleep", lambda _s: None)

    _, (mcp, _warnings) = _probe_mcp_via_handler(git_repo)

    assert mcp.mcp_status == "warming", (
        "WORKSTATE-REF-06 regression pin: cold-start (fail-then-succeed) must yield "
        f"mcp_status='warming', not {mcp.mcp_status!r}."
    )
    assert mcp.handoff_reachable is True, (
        "WORKSTATE-REF-06 regression pin: warming must keep handoff_reachable=True "
        "(derived back-compat view)."
    )


def test_doctor_probe_genuine_outage_reports_unreachable_after_retry_budget(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKSTATE-REF-06 deliverable: genuine outage exhausts the bounded retry budget
    and surfaces ``unreachable`` plus a restart-flavoured next_command."""
    from workstate.lifecycle.handlers import _common as common_mod  # noqa: PLC0415
    from workstate.lifecycle.handlers import doctor as doctor_handler  # noqa: PLC0415
    from receipts import DoctorBranch, ReceiptWarning  # noqa: PLC0415

    attempts = {"count": 0}

    def _fake_handoff(repo, *, argv, timeout_seconds, field):
        attempts["count"] += 1
        return None, ReceiptWarning(
            field=field, reason="timeout", exception_type="TimeoutExpired"
        )

    monkeypatch.setattr(common_mod, "run_handoff_json", _fake_handoff)
    _stub_orchestrator_unreachable(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/true")
    monkeypatch.setattr("time.sleep", lambda _s: None)

    _, (mcp, _warnings) = _probe_mcp_via_handler(git_repo)

    assert mcp.mcp_status == "unreachable", (
        "WORKSTATE-REF-06 regression pin: persistent failure must yield "
        f"mcp_status='unreachable', not {mcp.mcp_status!r}."
    )
    assert 2 <= attempts["count"] <= 8, (
        "WORKSTATE-REF-06 regression pin: retry budget must stay bounded "
        f"(2..8 attempts); got {attempts['count']}."
    )

    branch = DoctorBranch(name="main", head="abcdef", ahead_of_main=None, dirty=0)
    nxt = doctor_handler._suggest_next(branch, mcp)
    assert (
        "restart" in nxt.command.lower() and "MCP_WORKSTATE_HANDOFF_BIN" in nxt.command
    ), (
        "WORKSTATE-REF-06 regression pin: unreachable handoff next_command must "
        f"recommend the restart remediation; got {nxt.command!r}."
    )


def test_doctor_orchestrator_state_no_longer_hardcoded(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKSTATE-REF-06 deliverable: orchestrator_reachable must derive from a real
    probe, not the pre-WORKSTATE-REF-06 hardcoded ``False``."""
    from workstate.lifecycle.handlers import _common as common_mod  # noqa: PLC0415

    def _fake_handoff(repo, *, argv, timeout_seconds, field):
        return ({"ok": True, "data": {"active": {}}}, None)

    def _fake_orchestrator(repo, *, argv, timeout_seconds, field):
        return ({"ok": True, "status": "running"}, None)

    monkeypatch.setattr(common_mod, "run_handoff_json", _fake_handoff)
    monkeypatch.setattr(common_mod, "run_orchestrator_json", _fake_orchestrator)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/true")

    _, (mcp, _warnings) = _probe_mcp_via_handler(git_repo)

    assert mcp.orchestrator_status == "reachable", (
        "WORKSTATE-REF-06 regression pin: with a working orchestrator probe the "
        f"status must be 'reachable', not {mcp.orchestrator_status!r}."
    )
    assert mcp.orchestrator_reachable is True, (
        "WORKSTATE-REF-06 regression pin: orchestrator_reachable must derive True "
        "from orchestrator_status='reachable' (no longer hardcoded False)."
    )


# ---------------------------------------------------------------------------
# WORKSTATE-REF-80 implementation note: lifecycle doctor hook-wiring visibility.
#
# `_probe_hooks` previously returned an empty `DoctorHooks`, so `make doctor`
# could not explain whether the compact-session Stop adapters are installed or
# whether the repo's hoisted git-hook scripts are present in the inspected
# checkout. These tests pin (a) optional-not-installed adapter reporting, (b)
# installed-adapter detection, (c) hoisted git-hook readiness vs missing-hoist
# drift, and (d) backward-compatible JSON keys.
# ---------------------------------------------------------------------------

_VSCODE_ADAPTER_TARGET = ".vscode/workstate-stop-hooks.json"


def _probe_hooks_via_handler(
    repo: Path,
    *,
    env_findings: list[dict[str, object]] | None = None,
):
    """Import the handler lazily so the sys.path tweak above is honored."""
    from workstate.lifecycle.handlers import doctor as doctor_handler  # noqa: PLC0415

    return doctor_handler, doctor_handler._probe_hooks(repo, env_findings=env_findings)


def _set_hooks_path(repo: Path, value: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", value], check=True
    )


def _write_managed_stop_adapter(repo: Path, target: str) -> None:
    path = repo / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "_managed_by": "workstate-bootstrap",
                            "command": f"{repo}/scripts/hooks/compact-session.py",
                        }
                    ]
                }
            }
        )
    )


def test_WORKSTATE80_hooks_facet_reports_optional_not_installed_when_no_adapters(
    git_repo: Path,
) -> None:
    """With no adapter files present, every declared adapter is optional-not-installed."""
    _, hooks = _probe_hooks_via_handler(git_repo)

    assert hooks.stop_adapters_available, (
        "manifest compact-session adapters must be discovered"
    )
    assert hooks.stop_adapters_installed == []
    assert sorted(hooks.stop_adapters_optional_not_installed) == sorted(
        hooks.stop_adapters_available
    )
    assert any("install" in r.lower() for r in hooks.remediation), (
        "optional-not-installed adapters must surface an install remediation hint"
    )


def test_WORKSTATE80_hooks_facet_detects_installed_vscode_adapter(git_repo: Path) -> None:
    """A managed VS Code Stop adapter file is reported installed, not optional."""
    _write_managed_stop_adapter(git_repo, _VSCODE_ADAPTER_TARGET)

    _, hooks = _probe_hooks_via_handler(git_repo)

    assert any(_VSCODE_ADAPTER_TARGET in a for a in hooks.stop_adapters_installed), (
        f"installed VS Code adapter missing from {hooks.stop_adapters_installed!r}"
    )
    assert not any(
        _VSCODE_ADAPTER_TARGET in a for a in hooks.stop_adapters_optional_not_installed
    ), "an installed adapter must not also appear as optional-not-installed"


def test_WORKSTATE80_hooks_facet_reports_managed_adapter_drift_not_optional(
    git_repo: Path,
) -> None:
    """Bootstrap-reported managed drift takes precedence over optional missing state."""
    _, hooks = _probe_hooks_via_handler(
        git_repo,
        env_findings=[{"kind": "hook_adapter_drift", "path": _VSCODE_ADAPTER_TARGET}],
    )

    assert any(_VSCODE_ADAPTER_TARGET in a for a in hooks.stop_adapters_drifted), (
        "a managed drift finding must classify the adapter as drifted"
    )
    assert not any(
        _VSCODE_ADAPTER_TARGET in a for a in hooks.stop_adapters_installed
    ), "a drifted adapter must not also appear as installed"
    assert not any(
        _VSCODE_ADAPTER_TARGET in a for a in hooks.stop_adapters_optional_not_installed
    ), "a previously managed drifted adapter must not be reported as never opted in"
    assert any("repair" in r.lower() for r in hooks.remediation), (
        "drifted adapters must route operators to repair rather than first-time install"
    )


def test_WORKSTATE80_hooks_facet_reports_missing_hoisted_git_hooks(git_repo: Path) -> None:
    """core.hooksPath set but scripts absent => missing-hoist drift, not hoisted."""
    _set_hooks_path(git_repo, "scripts/hooks/git")

    _, hooks = _probe_hooks_via_handler(git_repo)

    assert hooks.git_hooks_path == "scripts/hooks/git"
    assert hooks.expected, (
        "expected hoisted git-hook names must be populated when hooksPath is set"
    )
    assert hooks.drift, "missing hoisted git-hook scripts must produce drift"
    assert hooks.git_hooks_hoisted is False


def test_WORKSTATE80_hooks_facet_hoisted_git_hooks_present(git_repo: Path) -> None:
    """core.hooksPath set and every expected script present => hoisted, no drift."""
    _set_hooks_path(git_repo, "scripts/hooks/git")
    _, hooks_probe = _probe_hooks_via_handler(git_repo)
    hooks_dir = git_repo / "scripts" / "hooks" / "git"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in hooks_probe.expected:
        (hooks_dir / name).write_text("#!/bin/sh\n")

    _, hooks = _probe_hooks_via_handler(git_repo)

    assert sorted(hooks.actual) == sorted(hooks.expected)
    assert hooks.drift == []
    assert hooks.git_hooks_hoisted is True


def test_WORKSTATE80_doctor_json_hooks_facet_preserves_legacy_keys(git_repo: Path) -> None:
    """JSON receipt keeps legacy expected/actual/drift keys and adds the new fields."""
    proc = _run_doctor(git_repo, "--json")

    assert proc.returncode == 0, proc.stderr
    hooks = json.loads(proc.stdout)["hooks"]
    for legacy_key in ("expected", "actual", "drift"):
        assert legacy_key in hooks, (
            f"legacy hooks key {legacy_key!r} dropped: {sorted(hooks)}"
        )
    for new_key in (
        "stop_adapters_available",
        "stop_adapters_installed",
        "stop_adapters_drifted",
        "stop_adapters_optional_not_installed",
        "git_hooks_path",
        "git_hooks_hoisted",
        "remediation",
    ):
        assert new_key in hooks, (
            f"new WORKSTATE-REF-80 hooks key {new_key!r} missing: {sorted(hooks)}"
        )


# ---------------------------------------------------------------------------
# WORKSTATE-REF-07 follow-up: doctor venv / pytest-resolution facet.
#
# The root .venv that task-start provisions only helps if a bare `pytest` from
# the worktree resolves to it rather than to a pyenv shim outside the tree.
# This facet reports (a) whether the worktree's root .venv/bin/pytest exists
# and (b) whether an ambient `pytest` on PATH resolves *outside* the worktree
# (the shim trap the root venv exists to prevent). `_probe_venv` is a pure
# filesystem + PATH read, so it is exercised directly with monkeypatched
# `shutil.which`.
# ---------------------------------------------------------------------------


def _probe_venv_via_handler(repo: Path):
    """Import the handler lazily so the sys.path tweak above is honored."""
    from workstate.lifecycle.handlers import doctor as doctor_handler  # noqa: PLC0415

    return doctor_handler, doctor_handler._probe_venv(repo)


def _make_root_venv_pytest(repo: Path) -> Path:
    """Create repo/.venv/bin/pytest and return its path."""
    pytest_path = repo / ".venv" / "bin" / "pytest"
    pytest_path.parent.mkdir(parents=True, exist_ok=True)
    pytest_path.write_text("#!/bin/sh\n", encoding="utf-8")
    pytest_path.chmod(0o755)
    return pytest_path


def test_probe_venv_reports_root_pytest_present(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root .venv/bin/pytest present + ambient pytest inside it => no remediation."""
    from receipts import DoctorVenv  # noqa: PLC0415

    pytest_path = _make_root_venv_pytest(git_repo)
    monkeypatch.setattr("shutil.which", lambda _name: str(pytest_path))

    _, venv = _probe_venv_via_handler(git_repo)

    assert isinstance(venv, DoctorVenv)
    assert venv.root_venv_present is True
    assert venv.root_venv_pytest_present is True
    assert venv.ambient_pytest_path == str(pytest_path)
    assert venv.ambient_pytest_outside_worktree is False
    assert venv.remediation == []


def test_probe_venv_absent_root_pytest_yields_remediation(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No root .venv/bin/pytest => present flags False with a provision hint."""
    monkeypatch.setattr("shutil.which", lambda _name: None)

    _, venv = _probe_venv_via_handler(git_repo)

    assert venv.root_venv_present is False
    assert venv.root_venv_pytest_present is False
    assert venv.ambient_pytest_path is None
    # No ambient pytest at all is unknown territory, not a confirmed shim risk.
    assert venv.ambient_pytest_outside_worktree is None
    assert any("provision" in line.lower() for line in venv.remediation), (
        venv.remediation
    )


def test_probe_venv_flags_ambient_pytest_outside_worktree(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ambient pytest resolving outside the worktree is the actionable shim signal."""
    _make_root_venv_pytest(git_repo)
    # A pyenv-shim-style path well outside the worktree.
    monkeypatch.setattr(
        "shutil.which", lambda _name: "/Users/somebody/.pyenv/shims/pytest"
    )

    _, venv = _probe_venv_via_handler(git_repo)

    assert venv.ambient_pytest_outside_worktree is True
    assert any(
        "outside" in line.lower() or "shim" in line.lower() for line in venv.remediation
    ), venv.remediation


def test_doctor_json_receipt_includes_venv_facet(git_repo: Path) -> None:
    """The JSON receipt must publish the venv facet so callers can read it."""
    proc = _run_doctor(git_repo, "--json")

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "venv" in receipt, f"venv facet missing from receipt: {sorted(receipt)}"
    for key in (
        "root_venv_present",
        "root_venv_pytest_present",
        "ambient_pytest_path",
        "ambient_pytest_outside_worktree",
        "remediation",
    ):
        assert key in receipt["venv"], (
            f"venv facet key {key!r} missing: {sorted(receipt['venv'])}"
        )


def test_doctor_human_summary_includes_venv_line(git_repo: Path) -> None:
    """WORKSTATE-REF-07-followups review fix: the non-JSON summary surfaces a `venv:` line.

    Pins the operator-facing stderr rendering (the JSON facet is covered
    separately) so the ambient_state branching cannot regress silently.
    """
    proc = _run_doctor(git_repo)

    assert proc.returncode == 0, proc.stderr
    venv_lines = [ln for ln in proc.stderr.splitlines() if ln.startswith("venv:")]
    assert venv_lines, f"no `venv:` line in doctor stderr: {proc.stderr!r}"
    line = venv_lines[0]
    assert "root_pytest=" in line and "ambient_pytest=" in line, line
    assert any(
        token in line for token in ("inside-worktree", "outside-worktree", "none")
    ), line
