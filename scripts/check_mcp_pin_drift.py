#!/usr/bin/env python3
"""Guard that managed MCP-server ``uvx`` pins do not silently drift.

The two Workstate MCP servers ship their consumer launch specs as
hand-maintained ``<distribution>@<version>`` pins in **two intentionally
separate** sites (different consumers — live developer config vs. emitted
plugin manifests — that may diverge later; see the header comment in
``mcp_servers.yaml``). Neither site is touched by
``scripts/release_prepare.py``'s dependency-floor rewriter, so bumping a
server package does not move its pins.

That gap shipped a real bug in the ``v0.1.22`` release: ``mcp-workstate-orchestrator``
was bumped ``0.5.0 -> 0.5.1`` but both pin sites kept saying ``@0.5.0``, so
package-source / default-server installs launched the superseded ``0.5.0``
wheel. A follow-up ``v0.1.23`` release was needed to correct the pins.

This module makes that drift impossible to ship silently. It verifies that
for any *managed* server (one that is pinned in a primary pin site) every
surface agrees with the expected version:

Primary pin sites (authoritative):
  1. ``packages/workstate-bootstrap/src/workstate_bootstrap/install.py``
     — ``DEFAULT_MCP_SERVERS[...]["args"][0] == "<distribution>@<version>"``
  2. ``packages/workstate-system/config/agent-workflows/mcp_servers.yaml``
     — ``mcp_servers[].args[0] == "<distribution>@<version>"``

Coupled surface (also verified so a stale doc cannot ship):
  3. ``packages/workstate-system/docs/plugin-distribution.md``
     — the operator-copyable ``<distribution>@<version>`` JSON snippets.

Two entry points:

* ``check_release_bump(repo_root, package_name, new_version)`` — used by
  ``scripts/release_prepare.py`` to FAIL a managed-server bump when the new
  version does not already match every pin surface.
* ``main()`` / ``make check-mcp-pins`` — standalone steady-state check
  (usable in CI / preflight). With no ``--package`` it checks every managed
  server against its own published (``pyproject.toml``) version.

A mismatch is **reported, never auto-rewritten**: updating a pin is coupled
to updating its drift-guard tests and ``docs/plugin-distribution.md``, and a
blind rewrite would leave those out of sync. The operator changes all of
them deliberately.

Detection is **deliberately conservative**: each surface is scanned for *every*
``<distribution>@<version>`` occurrence (not just the load-bearing
``args[0]``), and a surface is OK only when every occurrence agrees with the
expected version. So a stale version mentioned anywhere in a primary site —
even in a comment or an unrelated snippet — is treated as drift. This is
fail-closed by design: for a release guard, loudly flagging a stray version
reference (the operator removes or updates it) is safer than parsing only one
position and missing a real second pin.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_VERSION_RE = re.compile(r'(?m)^version\s*=\s*"([^"]+)"\s*$')

# Coupled surfaces an operator must update in lockstep with any pin change.
# Surfaced in the failure message so the change is made deliberately rather
# than silently rewritten.
COUPLED_DRIFT_GUARDS: tuple[str, ...] = (
    "packages/workstate-bootstrap/tests/test_install.py",
    "packages/workstate-bootstrap/tests/test_bootstrap_install_rehearsal.py",
    "packages/workstate-system/tests/test_plugin_emission.py",
    "packages/workstate-system/tests/test_plugin_distribution_doc.py",
)


@dataclass(frozen=True)
class PinSurface:
    """A file that carries a ``<distribution>@<version>`` pin for a server."""

    label: str
    relpath: str
    # The two authoritative pin sites are ``primary``; the operator-facing doc
    # is a coupled surface (also checked, but a server is only classified as
    # "managed" by its presence in a primary site).
    primary: bool


PIN_SURFACES: tuple[PinSurface, ...] = (
    PinSurface(
        label="DEFAULT_MCP_SERVERS",
        relpath="packages/workstate-bootstrap/src/workstate_bootstrap/install.py",
        primary=True,
    ),
    PinSurface(
        label="mcp_servers.yaml",
        relpath="packages/workstate-system/config/agent-workflows/mcp_servers.yaml",
        primary=True,
    ),
    PinSurface(
        label="plugin-distribution.md (coupled doc)",
        relpath="packages/workstate-system/docs/plugin-distribution.md",
        primary=False,
    ),
)


@dataclass(frozen=True)
class SurfaceFinding:
    surface: PinSurface
    found_versions: tuple[str, ...]
    ok: bool
    note: str


def _pin_re(distribution: str) -> re.Pattern[str]:
    # Anchor on the literal ``<distribution>@`` then a semver triple. The ``@``
    # boundary prevents one distribution name matching a longer sibling, and the
    # trailing ``(?![\w.])`` boundary stops a pre-release / 4-component pin
    # (``@0.5.2rc1``, ``@0.5.2.1``) from silently truncating to the base triple
    # and passing the guard — such a pin is a non-match, which check_distribution
    # then reports as "no pin" (drift) rather than a false OK.
    return re.compile(re.escape(distribution) + r"@(\d+\.\d+\.\d+)(?![\w.])")


def versions_in_text(text: str, distribution: str) -> list[str]:
    """All distinct ``<distribution>@<version>`` versions found, in order."""
    seen: list[str] = []
    for version in _pin_re(distribution).findall(text):
        if version not in seen:
            seen.append(version)
    return seen


def _read_surface(repo_root: Path, surface: PinSurface) -> str | None:
    path = repo_root / surface.relpath
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def load_packages(repo_root: Path) -> list[dict[str, object]]:
    manifest_path = repo_root / "config" / "release" / "packages.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    packages = data.get("packages")
    if not isinstance(packages, list):
        raise SystemExit(f"manifest must contain a 'packages' list: {manifest_path}")
    return packages


def distribution_for_package(repo_root: Path, package_name: str) -> str | None:
    for package in load_packages(repo_root):
        if package.get("name") == package_name:
            distribution = package.get("distribution")
            return str(distribution) if distribution is not None else None
    return None


def pyproject_version_for_distribution(
    repo_root: Path, distribution: str
) -> str | None:
    for package in load_packages(repo_root):
        if package.get("distribution") != distribution:
            continue
        pyproject_path = repo_root / str(package["path"]) / "pyproject.toml"
        if not pyproject_path.is_file():
            return None
        match = PYPROJECT_VERSION_RE.search(pyproject_path.read_text(encoding="utf-8"))
        return match.group(1) if match else None
    return None


def managed_distributions(repo_root: Path) -> dict[str, str]:
    """Map ``distribution -> package name`` for every server pinned in a
    *primary* pin site. A server is "managed" (and therefore guarded) iff it
    appears in at least one authoritative pin site."""
    dist_to_pkg = {
        str(p["distribution"]): str(p["name"])
        for p in load_packages(repo_root)
        if p.get("distribution") and p.get("name")
    }
    texts = {
        surface.label: _read_surface(repo_root, surface) for surface in PIN_SURFACES
    }
    managed: dict[str, str] = {}
    for distribution, package_name in dist_to_pkg.items():
        for surface in PIN_SURFACES:
            if not surface.primary:
                continue
            text = texts[surface.label]
            if text is not None and versions_in_text(text, distribution):
                managed[distribution] = package_name
                break
    return managed


def check_distribution(
    repo_root: Path, distribution: str, expected_version: str
) -> list[SurfaceFinding]:
    findings: list[SurfaceFinding] = []
    for surface in PIN_SURFACES:
        text = _read_surface(repo_root, surface)
        if text is None:
            # A primary pin site that is simply absent is reported; the coupled
            # doc being absent is tolerated (some checkouts may not ship it).
            findings.append(
                SurfaceFinding(
                    surface=surface,
                    found_versions=(),
                    ok=not surface.primary,
                    note="file not found" if surface.primary else "absent (skipped)",
                )
            )
            continue
        versions = versions_in_text(text, distribution)
        if not versions:
            findings.append(
                SurfaceFinding(
                    surface=surface,
                    found_versions=(),
                    ok=False,
                    note=f"no pin for {distribution} (expected {expected_version})",
                )
            )
        elif set(versions) != {expected_version}:
            findings.append(
                SurfaceFinding(
                    surface=surface,
                    found_versions=tuple(versions),
                    ok=False,
                    note=f"pins {', '.join(versions)} (expected {expected_version}) [STALE]",
                )
            )
        else:
            findings.append(
                SurfaceFinding(
                    surface=surface,
                    found_versions=tuple(versions),
                    ok=True,
                    note=f"pins {expected_version}",
                )
            )
    return findings


def _format_drift(
    distribution: str, expected_version: str, findings: list[SurfaceFinding]
) -> list[str]:
    lines = [f"MCP server pin drift for {distribution} (expected {expected_version}):"]
    for finding in findings:
        marker = "ok " if finding.ok else "DRIFT"
        lines.append(
            f"  [{marker}] {finding.surface.label} "
            f"({finding.surface.relpath}): {finding.note}"
        )
    return lines


def _coupling_reminder(expected_version: str) -> list[str]:
    lines = [
        "",
        "Pins are intentionally NOT auto-rewritten (the two sites serve "
        "different consumers; see the mcp_servers.yaml header).",
        f"Update each stale pin to {expected_version}, then update the coupled "
        "drift-guard tests + doc in the same change so they do not fall out of "
        "sync:",
    ]
    lines.extend(f"  - {path}" for path in COUPLED_DRIFT_GUARDS)
    return lines


def check_release_bump(
    repo_root: Path, package_name: str, new_version: str
) -> tuple[bool, list[str]]:
    """Gate a release bump: if ``package_name`` is a managed MCP server, every
    pin surface must already agree with ``new_version``.

    Returns ``(ok, messages)``. Non-server packages are a no-op (``True, []``)
    so this can be called unconditionally for any release bump.
    """
    distribution = distribution_for_package(repo_root, package_name)
    if distribution is None:
        return True, []
    if distribution not in managed_distributions(repo_root):
        return True, []

    findings = check_distribution(repo_root, distribution, new_version)
    if all(finding.ok for finding in findings):
        return True, []

    messages = [
        f"Refusing to bump {package_name} to {new_version}: managed MCP-server "
        "pins are stale.",
        *_format_drift(distribution, new_version, findings),
        *_coupling_reminder(new_version),
    ]
    return False, messages


def check_all(
    repo_root: Path, package_name: str | None = None
) -> tuple[bool, list[str]]:
    """Steady-state check: every managed server's pins must agree with its own
    published (``pyproject.toml``) version. Optionally scope to one package.

    Returns ``(ok, messages)``; ``messages`` always carries a one-line status
    summary even on success.
    """
    managed = managed_distributions(repo_root)

    if package_name is not None:
        distribution = distribution_for_package(repo_root, package_name)
        if distribution is None:
            return False, [f"unknown package in release manifest: {package_name}"]
        if distribution not in managed:
            return True, [
                f"{package_name} ({distribution}) is not a managed MCP server; "
                "nothing to check."
            ]
        managed = {distribution: package_name}

    if not managed:
        return True, ["no managed MCP servers found to check"]

    ok = True
    messages: list[str] = []
    for distribution in sorted(managed):
        expected = pyproject_version_for_distribution(repo_root, distribution)
        if expected is None:
            ok = False
            messages.append(
                f"could not resolve published version for {distribution} "
                "(missing pyproject.toml or version field)"
            )
            continue
        findings = check_distribution(repo_root, distribution, expected)
        if all(finding.ok for finding in findings):
            messages.append(
                f"ok: {distribution} pinned at {expected} across all surfaces"
            )
            continue
        ok = False
        messages.extend(_format_drift(distribution, expected, findings))
        messages.extend(_coupling_reminder(expected))

    return ok, messages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify managed MCP-server uvx pins agree across both pin sites "
            "and the published version."
        )
    )
    parser.add_argument(
        "--package",
        default=None,
        help="Limit the check to a single managed-server package name.",
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Repository root (defaults to the monorepo containing this script).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    ok, messages = check_all(repo_root, package_name=args.package)
    stream = sys.stdout if ok else sys.stderr
    for line in messages:
        print(line, file=stream)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
