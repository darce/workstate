"""implementation note implementation note — coherence validator core.

Fixtures model the four live cases the plan names:

- the terminal-guard incident (stale clone config names a payload-deleted
  script through mixed symlinked surfaces),
- the consumer committed-orphan ``.codex/hooks.json``,
- the hybrid receipt (``source_kind=package`` + clone symlinks — the exact
  monorepo substrate),
- the non-hook shared-surface skew (stale ``Makefile.d`` vs live hooks).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
import sys

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PACKAGE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
for name in list(sys.modules):
    if name == "workstate_bootstrap" or name.startswith("workstate_bootstrap."):
        sys.modules.pop(name)

from workstate_bootstrap.coherence import (
    CoherenceFinding,
    assess_hook_coherence,
)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        cwd=cwd,
    ).stdout.strip()


def _make_clone(target: Path, files: dict[str, str]) -> str:
    """Create the ``.workstate/remote`` git clone with ``files`` committed.

    Returns the clone HEAD sha.
    """
    clone = target / ".workstate" / "remote"
    clone.mkdir(parents=True)
    _git("init", "-q", cwd=clone)
    _git("config", "user.email", "t@example.com", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    for rel, content in files.items():
        path = clone / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "payload", cwd=clone)
    return _git("rev-parse", "HEAD", cwd=clone)


def _write_receipt(
    target: Path,
    *,
    source_kind: str = "git_overlay",
    surfaces: list[str],
    configs: list[str] | None = None,
    remote_sha: str | None = None,
    local_surfaces: list[str] | None = None,
) -> None:
    local = set(local_surfaces or [])
    receipt: dict[str, object] = {
        "schema_version": 2,
        "profile": "all",
        "surfaces": [
            {"path": s, "source": "local" if s in local else "shared"}
            for s in surfaces
        ],
        "configs": [{"path": c, "action": "updated"} for c in configs or []],
        "mcp_servers": [],
    }
    if source_kind == "package":
        receipt["source_kind"] = "package"
        receipt["package_version"] = "0.0.0+test"
    else:
        receipt["remote_url"] = "https://example.invalid/overlay.git"
        receipt["remote_ref"] = "main"
        receipt["remote_sha"] = remote_sha
    (target / ".workstate-bootstrap.json").write_text(json.dumps(receipt))


def _symlink(target: Path, rel: str, to: Path) -> None:
    link = target / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(to)


def _vscode_config(commands: list[str]) -> str:
    return json.dumps(
        {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "type": "command", "command": c}
                    for c in commands
                ]
            }
        }
    )


def _kinds(findings: list[CoherenceFinding]) -> set[str]:
    return {f.kind for f in findings}


def _by_kind(findings: list[CoherenceFinding], kind: str) -> list[CoherenceFinding]:
    return [f for f in findings if f.kind == kind]


@pytest.fixture
def coherent_overlay(tmp_path: Path) -> tuple[Path, str]:
    """git_overlay target whose both hook surfaces symlink the same clone and
    whose every config command resolves. Baseline for green/orphan tests."""
    target = tmp_path / "consumer"
    target.mkdir()
    head = _make_clone(
        target,
        {
            ".github/hooks/terminal-guard.json": _vscode_config(
                ["python3 scripts/hooks/guard-ok.py"]
            ),
            "scripts/hooks/guard-ok.py": "print('ok')\n",
        },
    )
    clone = target / ".workstate" / "remote"
    _symlink(target, ".github/hooks", clone / ".github" / "hooks")
    _symlink(target, "scripts/hooks", clone / "scripts" / "hooks")
    _write_receipt(
        target,
        surfaces=[".github/hooks", "scripts/hooks"],
        remote_sha=head,
    )
    return target, head


def test_all_coherent_green(coherent_overlay: tuple[Path, str]) -> None:
    target, _ = coherent_overlay
    assert assess_hook_coherence(target) == []


def test_live_incident_reproduced(tmp_path: Path) -> None:
    """The exact incident: package receipt; scripts/hooks symlinks the LIVE
    payload (terminal-guard.py deleted), .github/hooks symlinks the STALE
    clone whose config still invokes it."""
    target = tmp_path / "monorepo"
    target.mkdir()
    # Live payload: retirement already deleted terminal-guard.py.
    payload = tmp_path / "live-payload"
    (payload / "scripts" / "hooks").mkdir(parents=True)
    (payload / "scripts" / "hooks" / "guard-ok.py").write_text("print('ok')\n")
    # Stale clone: config still names the deleted script.
    _make_clone(
        target,
        {
            ".github/hooks/terminal-guard.json": _vscode_config(
                ["python3 scripts/hooks/terminal-guard.py"]
            ),
            "scripts/hooks/terminal-guard.py": "raise SystemExit(0)\n",
        },
    )
    clone = target / ".workstate" / "remote"
    _symlink(target, ".github/hooks", clone / ".github" / "hooks")
    _symlink(target, "scripts/hooks", payload / "scripts" / "hooks")
    _write_receipt(
        target,
        source_kind="package",
        surfaces=[".github/hooks", "scripts/hooks"],
    )

    findings = assess_hook_coherence(target, package_root=payload)

    unresolved = _by_kind(findings, "hook_command_unresolved")
    assert len(unresolved) == 1
    assert unresolved[0].severity == "error"
    assert "terminal-guard.py" in unresolved[0].detail

    skew = _by_kind(findings, "hook_surface_skew")
    assert [f.severity for f in skew] == ["error"]
    assert "clone:" in skew[0].detail and "link:" in skew[0].detail

    hybrid = _by_kind(findings, "hybrid_receipt")
    assert [f.severity for f in hybrid] == ["warning"]
    assert ".github/hooks" in hybrid[0].path


def test_consumer_committed_orphan_codex(
    coherent_overlay: tuple[Path, str],
) -> None:
    """A git-committed .codex/hooks.json outside the receipt and the renderer
    outputs is flagged orphan even when its commands resolve."""
    target, _ = coherent_overlay
    codex = target / ".codex" / "hooks.json"
    codex.parent.mkdir(parents=True)
    codex.write_text(_vscode_config(["python3 scripts/hooks/guard-ok.py"]))

    findings = assess_hook_coherence(target)

    assert _kinds(findings) == {"orphan_hook_config"}
    orphan = findings[0]
    assert orphan.severity == "warning"
    assert orphan.path == ".codex/hooks.json"


def test_receipt_known_config_not_orphan(
    coherent_overlay: tuple[Path, str],
) -> None:
    """A config recorded in receipt configs[] (e.g. a regenerated plugin-tree
    hooks config) is managed — never orphan, even under a gitignored path."""
    target, head = coherent_overlay
    plugin_cfg = target / ".workstate" / "generated" / "plugins" / "ws" / "hooks.json"
    plugin_cfg.parent.mkdir(parents=True)
    plugin_cfg.write_text(_vscode_config(["python3 scripts/hooks/guard-ok.py"]))
    _write_receipt(
        target,
        surfaces=[".github/hooks", "scripts/hooks"],
        configs=[".workstate/generated/plugins/ws/hooks.json"],
        remote_sha=head,
    )

    assert assess_hook_coherence(target) == []


def test_stale_clone_warn(coherent_overlay: tuple[Path, str]) -> None:
    target, head = coherent_overlay
    _write_receipt(
        target,
        surfaces=[".github/hooks", "scripts/hooks"],
        remote_sha="0" * 40,
    )

    findings = assess_hook_coherence(target)

    assert _kinds(findings) == {"clone_stale"}
    assert findings[0].severity == "warning"
    assert head[:12] in findings[0].detail


def test_non_hook_shared_surface_skew_warns(
    coherent_overlay: tuple[Path, str], tmp_path: Path
) -> None:
    """Hook surfaces coherent, but Makefile.d symlinks a different snapshot:
    WARN, not ERROR — a stale recipe degrades, it cannot fail-close."""
    target, head = coherent_overlay
    elsewhere = tmp_path / "stale-elsewhere" / "Makefile.d"
    elsewhere.mkdir(parents=True)
    (elsewhere / "plans.mk").write_text("plan-accept:\n\t@true\n")
    _symlink(target, "Makefile.d", elsewhere)
    _write_receipt(
        target,
        surfaces=[".github/hooks", "scripts/hooks", "Makefile.d"],
        remote_sha=head,
    )

    findings = assess_hook_coherence(target)

    skew = _by_kind(findings, "hook_surface_skew")
    assert [f.severity for f in skew] == ["warning"]
    assert skew[0].path == "Makefile.d"
    assert _kinds(findings) == {"hook_surface_skew"}


def test_operator_local_hook_surface_skew_warns_not_errors(
    coherent_overlay: tuple[Path, str], tmp_path: Path
) -> None:
    """A hook surface the receipt classifies ``source='local'`` is
    operator-owned: install deliberately preserved a foreign mount. Its
    snapshot divergence from the managed hook surfaces must WARN, never
    ERROR — implementation note's install/update gate aborts on ERROR and would
    otherwise refuse the documented preserve-foreign-as-local contract."""
    target, head = coherent_overlay
    foreign = tmp_path / "operator-overlay" / "hooks"
    foreign.mkdir(parents=True)
    (foreign / "guard-ok.py").write_text("print('ok')\n")
    (target / "scripts" / "hooks").unlink()
    _symlink(target, "scripts/hooks", foreign)
    _write_receipt(
        target,
        surfaces=[".github/hooks", "scripts/hooks"],
        remote_sha=head,
        local_surfaces=["scripts/hooks"],
    )

    findings = assess_hook_coherence(target)

    assert all(f.severity == "warning" for f in findings), findings
    skew = _by_kind(findings, "hook_surface_skew")
    assert len(skew) == 1
    assert skew[0].path == "scripts/hooks"
    assert "operator-owned" in skew[0].detail


def test_wrapper_two_path_resolution(
    coherent_overlay: tuple[Path, str],
) -> None:
    """implementation note wrapper form: BOTH the wrapper path and the handler relpath
    argument must resolve; a dangling handler behind a healthy wrapper is
    still an ERROR."""
    target, _ = coherent_overlay
    hooks_dir = (target / ".github" / "hooks").resolve()
    wrapper = target / "scripts" / "hooks" / "_run_guard.py"
    # scripts/hooks is a symlink into the clone; write through it.
    wrapper.parent.resolve().joinpath("_run_guard.py").write_text("pass\n")
    (hooks_dir / "terminal-guard.json").write_text(
        _vscode_config(
            [
                "python3 scripts/hooks/_run_guard.py scripts/hooks/guard-ok.py",
                "python3 scripts/hooks/_run_guard.py scripts/hooks/gone.py",
            ]
        )
    )

    findings = assess_hook_coherence(target)

    unresolved = _by_kind(findings, "hook_command_unresolved")
    assert len(unresolved) == 1
    assert "gone.py" in unresolved[0].detail


def test_unreadable_config_is_error(coherent_overlay: tuple[Path, str]) -> None:
    target, _ = coherent_overlay
    bad = (target / ".github" / "hooks").resolve() / "terminal-guard.json"
    bad.write_text("{not json")

    findings = assess_hook_coherence(target)

    unresolved = _by_kind(findings, "hook_command_unresolved")
    assert len(unresolved) == 1
    assert "unreadable" in unresolved[0].detail


def test_no_receipt_still_resolves_scripts(tmp_path: Path) -> None:
    """Without a receipt the provenance gates skip, but the resolve gate
    still protects the harness (and unknown configs read as orphans)."""
    target = tmp_path / "bare"
    (target / ".github" / "hooks").mkdir(parents=True)
    (target / ".github" / "hooks" / "terminal-guard.json").write_text(
        _vscode_config(["python3 scripts/hooks/missing.py"])
    )

    findings = assess_hook_coherence(target)

    assert _kinds(findings) == {"hook_command_unresolved", "orphan_hook_config"}
