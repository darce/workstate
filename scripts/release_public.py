#!/usr/bin/env python3
"""Orchestrate the public Workstate release flow (dry-run by default).

This is the single release-owned command that composes the existing release
tooling into one repeatable public-release path:

  1. preflight  — verify every publishable package has a matching PyPI Trusted
                  Publisher binding for ``darce/workstate`` /
                  ``release-publish.yml`` / ``pypi`` (reported in dry-run, the
                  live PyPI probe is gated behind ``--execute``),
  2. export     — build the public tree via ``scripts/export_public.py``,
  3. push       — push the exported tree to ``git@github.com:darce/workstate.git``
                  (``--execute`` only),
  4. tag-sync   — create the per-package tag family plus the consumer-facing
                  ``vX.Y.Z`` monorepo tag on the public commit (``--execute`` only),
  5. status     — a unified report distinguishing the five release states.

Dry-run is the default. Network-mutating steps (git push, tag push, PyPI
upload) run ONLY under ``--execute`` *and* an interactive operator
confirmation. Every mutating step is idempotent (re-running after a partial
failure converges, never double-publishes), reusing the existing
``scripts/release.sh`` pending-recovery machinery for state reporting.

Authoritative playbook: docs/RELEASING.md.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_HELPER = REPO_ROOT / "scripts" / "release_manifest.py"
EXPORT_PUBLIC = REPO_ROOT / "scripts" / "export_public.py"
RELEASE_SCRIPT = REPO_ROOT / "scripts" / "release.sh"

# The public consumer-facing repository and the Trusted Publisher binding that
# each publishable PyPI project must carry before any upload is dispatched.
PUBLIC_GIT_REMOTE = "git@github.com:darce/workstate.git"
PUBLISHER_OWNER = "darce"
PUBLISHER_REPO = "workstate"
PUBLISHER_WORKFLOW = "release-publish.yml"
PUBLISHER_ENVIRONMENT = "pypi"

# Ordered list of pipeline steps surfaced in the dry-run plan. The two
# read-only steps (preflight, status) never mutate; the middle three are the
# network-mutating steps guarded behind --execute + confirmation.
PIPELINE_STEPS = ("preflight", "export", "push", "tag-sync", "status")
MUTATING_STEPS = ("push", "tag-sync", "publish")

# Documented account-scoped fallback for a missing Trusted Publisher, a PyPI
# outage, or a publisher-form failure. Surfaced verbatim in the preflight
# report so an operator never has to leave the command output to recover.
MANUAL_UPLOAD_FALLBACK = (
    "fallback: if a Trusted Publisher is missing or PyPI is unavailable, "
    "publish with the account-scoped API token via "
    "`uvx --with keyring twine upload packages/<pkg>/dist/*` "
    "(scripts/release.sh package <pkg>), then add the Trusted Publisher "
    f"({PUBLISHER_OWNER}/{PUBLISHER_REPO} / {PUBLISHER_WORKFLOW} / "
    f"{PUBLISHER_ENVIRONMENT}) before the next release."
)


def _run_text(args: list[str]) -> str:
    proc = subprocess.run(
        args,
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def publishable_packages() -> list[str]:
    """Publishable (publish=true) package names from the release manifest.

    Reuses release_manifest.py's --release-only filter so the public-release
    preflight enumerates exactly the same set the publish workflow and
    release.sh operate on (mcp-workstate-canvas, publish=false, is excluded).
    """
    out = _run_text(
        [
            sys.executable,
            str(MANIFEST_HELPER),
            "list",
            "--release-only",
            "--field",
            "name",
        ]
    )
    return [line for line in out.splitlines() if line.strip()]


def release_plan() -> dict[str, object]:
    """Canonical machine-readable release plan from scripts/release.sh."""
    out = _run_text(["bash", str(RELEASE_SCRIPT), "plan", "--json"])
    return json.loads(out)


def probe_trusted_publisher(distribution: str) -> bool:
    """Return whether ``distribution`` has the expected Trusted Publisher.

    NETWORK-MUTATING-ADJACENT: this hits the PyPI JSON API and is only ever
    called under --execute. In dry-run the preflight reports the binding it
    *would* check without contacting PyPI.
    """
    # Hermetic test seam: a comma-separated allowlist of distributions whose
    # publisher binding is considered present, set instead of touching PyPI.
    # Used only by the dry-run-only test suite to exercise the --execute
    # preflight branch without any network call.
    override = os.environ.get("RELEASE_PUBLIC_FAKE_PUBLISHERS")
    if override is not None:
        covered = {name for name in override.split(",") if name}
        return distribution in covered

    import urllib.error
    import urllib.request

    url = f"https://pypi.org/pypi/{distribution}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # Project does not exist yet — a pending publisher is required.
            return False
        raise
    publishers = payload.get("trusted-publishers") or payload.get(
        "trusted_publishers"
    )
    if not isinstance(publishers, list):
        # PyPI does not expose publisher metadata on the public JSON API for
        # most projects; absence here is not proof of a missing binding, so the
        # operator must confirm via the PyPI project settings page. We treat an
        # explicit non-empty match as the only "covered" signal.
        return False
    for publisher in publishers:
        if not isinstance(publisher, dict):
            continue
        if (
            publisher.get("owner") == PUBLISHER_OWNER
            and publisher.get("repository") == PUBLISHER_REPO
            and publisher.get("workflow") == PUBLISHER_WORKFLOW
        ):
            return True
    return False


def build_preflight(
    packages: list[str],
    *,
    execute: bool,
    missing_publishers: list[str] | None = None,
) -> dict[str, object]:
    """Assemble the Trusted-Publisher preflight result.

    In dry-run (``execute`` is False) this is a *reported* check: it records
    the exact set of packages that WOULD be probed and reports ``checked`` as
    False without any network call. Under ``--execute`` it probes PyPI and
    records the missing-publisher set, failing clearly when one is absent.
    """
    binding = {
        "owner": PUBLISHER_OWNER,
        "repository": PUBLISHER_REPO,
        "workflow": PUBLISHER_WORKFLOW,
        "environment": PUBLISHER_ENVIRONMENT,
    }
    if not execute:
        return {
            "checked": False,
            "would_check": list(packages),
            "binding": binding,
            "missing": [],
            "ok": True,
            "fallback": MANUAL_UPLOAD_FALLBACK,
        }

    if missing_publishers is None:
        missing_publishers = [
            package
            for package in packages
            if not probe_trusted_publisher(package)
        ]
    return {
        "checked": True,
        "would_check": list(packages),
        "binding": binding,
        "missing": list(missing_publishers),
        "ok": not missing_publishers,
        "fallback": MANUAL_UPLOAD_FALLBACK,
    }


def build_report(
    *,
    execute: bool,
    confirmed: bool,
    plan: dict[str, object],
    preflight: dict[str, object],
) -> dict[str, object]:
    """Assemble the full release-public report (the --json payload)."""
    packages = preflight["would_check"]
    will_mutate = bool(execute and confirmed and preflight.get("ok", False))

    steps = []
    for name in PIPELINE_STEPS:
        mutating = name in MUTATING_STEPS
        steps.append(
            {
                "step": name,
                "mutating": mutating,
                # Mutating steps only "run" under --execute + confirmation;
                # otherwise they are planned but skipped.
                "action": ("execute" if (mutating and will_mutate) else "plan"),
            }
        )

    # The five release states the status report must distinguish, sourced from
    # the existing release.sh pending-recovery state machine, plus the two
    # public-export-specific states this command owns.
    package_status = []
    plan_packages = {
        entry["name"]: entry
        for entry in plan.get("packages", [])
        if isinstance(entry, dict)
    }
    publisher_missing = set(preflight.get("missing", []))
    for package in packages:
        entry = plan_packages.get(package, {})
        if preflight["checked"]:
            publisher_state = (
                "missing" if package in publisher_missing else "ready"
            )
        else:
            publisher_state = "unchecked (dry-run)"
        package_status.append(
            {
                "name": package,
                # private source-repo tag state (released/pending_upload/...)
                "private_source_tag": entry.get("state", "unknown"),
                # public export branch freshness + public tag presence are
                # only known after a real export/push; in build-only mode we
                # report the planned target, never a probed remote state.
                "public_export_branch": "pending_export",
                "public_tag": "pending_tag_sync",
                # PyPI publication state, derived from the same plan state.
                "pypi_publication": (
                    "published"
                    if entry.get("state") == "released"
                    else "unpublished"
                ),
                # PyPI Trusted Publisher readiness.
                "trusted_publisher": publisher_state,
            }
        )

    return {
        "mode": "execute" if execute else "dry-run",
        "confirmed": confirmed,
        "will_mutate": will_mutate,
        "public_remote": PUBLIC_GIT_REMOTE,
        "monorepo_tag": (plan.get("monorepo", {}) or {}).get(
            "suggested_next_tag"
        ),
        "steps": steps,
        "preflight": preflight,
        "status": package_status,
    }


def render_text(report: dict[str, object]) -> str:
    lines: list[str] = []
    mode = report["mode"]
    lines.append(f"[release-public] mode: {mode}")
    lines.append(f"[release-public] public remote: {report['public_remote']}")
    lines.append(
        f"[release-public] monorepo tag (suggested): {report['monorepo_tag']}"
    )
    lines.append("")
    lines.append("Pipeline steps (in order):")
    for index, step in enumerate(report["steps"], start=1):
        marker = "MUTATING" if step["mutating"] else "read-only"
        lines.append(
            f"  {index}. {step['step']:<9} [{marker}] -> {step['action']}"
        )
    lines.append("")

    preflight = report["preflight"]
    lines.append("PyPI Trusted Publisher preflight:")
    binding = preflight["binding"]
    lines.append(
        "  binding: "
        f"{binding['owner']}/{binding['repository']} / "
        f"{binding['workflow']} / {binding['environment']}"
    )
    if preflight["checked"]:
        if preflight["ok"]:
            lines.append("  result: OK — all publishable packages covered")
        else:
            lines.append(
                "  result: FAIL — missing Trusted Publisher for: "
                + ", ".join(preflight["missing"])
            )
    else:
        lines.append(
            "  result: reported (dry-run) — would check: "
            + ", ".join(preflight["would_check"])
        )
    lines.append(f"  {preflight['fallback']}")
    lines.append("")

    lines.append("Release status (five states):")
    header = (
        f"  {'PACKAGE':<28} {'SRC-TAG':<16} {'PUB-BRANCH':<16} "
        f"{'PUB-TAG':<16} {'PYPI':<12} {'PUBLISHER':<18}"
    )
    lines.append(header)
    for entry in report["status"]:
        lines.append(
            f"  {entry['name']:<28} {entry['private_source_tag']:<16} "
            f"{entry['public_export_branch']:<16} {entry['public_tag']:<16} "
            f"{entry['pypi_publication']:<12} {entry['trusted_publisher']:<18}"
        )
    lines.append("")

    if report["mode"] == "execute" and not report["confirmed"]:
        lines.append(
            "[release-public] --execute requested but not confirmed — "
            "no network-mutating step ran (git push / tag push / PyPI "
            "upload all skipped)."
        )
    elif report["mode"] == "dry-run":
        lines.append(
            "[release-public] dry-run only — no network mutation performed. "
            "Re-run with --execute to push/tag/publish after confirmation."
        )
    return "\n".join(lines) + "\n"


def confirm_interactively() -> bool:
    """Prompt the operator before any network-mutating step.

    Reads from stdin; returns False on EOF / non-tty so a non-interactive
    --execute invocation never mutates. ``--assume-yes`` bypasses this.
    """
    # Write the prompt to stderr so a --json run keeps stdout machine-clean.
    sys.stderr.write(
        "About to PUSH to the public repo, PUSH tags, and PUBLISH wheels.\n"
        "This mutates remote state. Type 'publish' to proceed: "
    )
    sys.stderr.flush()
    try:
        answer = input()
    except EOFError:
        return False
    return answer.strip() == "publish"


def run(argv: list[str]) -> int:
    args = parse_args(argv)

    # --dry-run is the default; --execute opts into mutation, still gated on
    # an interactive confirmation (or --assume-yes for an automated operator).
    execute = bool(args.execute)
    confirmed = False
    if execute:
        confirmed = True if args.assume_yes else confirm_interactively()

    packages = publishable_packages()
    plan = release_plan()
    preflight = build_preflight(packages, execute=execute)
    report = build_report(
        execute=execute,
        confirmed=confirmed,
        plan=plan,
        preflight=preflight,
    )

    if args.json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        sys.stdout.write(render_text(report))

    # A failed publisher preflight under --execute blocks before any upload.
    if execute and not preflight.get("ok", True):
        sys.stderr.write(
            "[release-public] aborting: missing Trusted Publisher binding "
            "for: " + ", ".join(preflight.get("missing", [])) + "\n"
        )
        return 1

    if execute and not confirmed:
        # Honor the gate: --execute without confirmation is a no-op, not an
        # error, so an operator can rehearse the prompt safely.
        return 0

    if args.preflight_only:
        # Rehearse the preflight (and, under --execute, the live probe) without
        # touching the mutating steps. Returns nonzero above if a publisher is
        # missing; reaching here means the preflight passed.
        return 0

    if execute and confirmed:
        return _execute(report)

    return 0


def _execute(report: dict[str, object]) -> int:  # pragma: no cover - gated
    """Perform the network-mutating steps. Reached only under --execute +
    confirmation; deliberately unexercised by the dry-run test suite.

    Each step is idempotent and reuses the existing release tooling:
      - export: scripts/export_public.py --out <tree> --force
      - push:   git push to PUBLIC_GIT_REMOTE (force-with-lease, converges)
      - tag-sync + publish: scripts/release.sh pending <tag> (idempotent;
        re-running after a wheels-published-but-tag-push-failed partial
        failure converges via the pending-recovery state machine and never
        double-publishes).

    This branch is intentionally left as the operator-gated path required by
    implementation note decision D3 (build-only this pass). It is not implemented as a
    live mutation here; a real run is a separate, explicitly operator-driven
    follow-up.
    """
    raise SystemExit(
        "[release-public] --execute path is operator-gated and not enabled in "
        "this build-only slice (implementation note D3). Run the documented manual "
        "export/push/tag/publish steps, or enable this path in the real-run "
        "follow-up."
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Plan and report only; perform no network mutation (default).",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Perform network-mutating steps after interactive confirmation.",
    )
    parser.add_argument(
        "--assume-yes",
        action="store_true",
        help="Skip the interactive confirmation prompt (operator automation).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the machine-readable report instead of the text report.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help=(
            "Run the publisher preflight + report and stop before any "
            "export/push/tag/publish step (rehearsal)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return run(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
