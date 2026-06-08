"""implementation note implementation note — doctor facet + CLI gate over the coherence module.

The facet must run for ALL source kinds INCLUDING ``source=local`` surfaces:
the terminal-guard incident lived entirely in ``source=local`` mounts that
``_doctor_local_surfaces`` validates per-surface, never cross-surface.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PACKAGE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
for name in list(sys.modules):
    if name == "workstate_bootstrap" or name.startswith("workstate_bootstrap."):
        sys.modules.pop(name)

from workstate_bootstrap.coherence import main as coherence_main
from workstate_bootstrap.subcommands import _doctor_hook_coherence, doctor


def _incident_target(tmp_path: Path) -> Path:
    """package-mode target whose source=local hook config names a missing
    script — the doctor-facet projection of the live incident."""
    target = tmp_path / "consumer"
    (target / ".github" / "hooks").mkdir(parents=True)
    (target / ".github" / "hooks" / "terminal-guard.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "type": "command",
                            "command": "python3 scripts/hooks/terminal-guard.py",
                        }
                    ]
                }
            }
        )
    )
    (target / "scripts" / "hooks").mkdir(parents=True)
    (target / ".workstate-bootstrap.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "source_kind": "package",
                "package_version": "0.0.0+test",
                "profile": "all",
                "surfaces": [
                    {"path": ".github/hooks", "source": "local"},
                    {"path": "scripts/hooks", "source": "local"},
                ],
                "configs": [],
                "mcp_servers": [],
            }
        )
    )
    return target


def test_facet_projects_coherence_findings(tmp_path: Path) -> None:
    target = _incident_target(tmp_path)

    rows = _doctor_hook_coherence(target)

    unresolved = [r for r in rows if r["kind"] == "hook_command_unresolved"]
    assert len(unresolved) == 1
    assert unresolved[0]["severity"] == "error"
    assert unresolved[0]["path"] == ".github/hooks/terminal-guard.json"
    assert "terminal-guard.py" in unresolved[0]["message"]


def test_doctor_runs_facet_for_local_surfaces(tmp_path: Path) -> None:
    """End-to-end: doctor() surfaces the coherence row even though every hook
    surface is source=local (the _doctor_local_surfaces blind spot)."""
    target = _incident_target(tmp_path)

    findings = doctor(target=target)

    kinds = {f["kind"] for f in findings}
    assert "hook_command_unresolved" in kinds


def test_cli_gate_exit_codes(tmp_path: Path, capsys) -> None:
    """error finding -> exit 1; warnings alone -> exit 0 (CI severity
    contract: offline stale-clone/hybrid rows never block)."""
    target = _incident_target(tmp_path)
    assert coherence_main([str(target)]) == 1
    out = capsys.readouterr().out
    assert "hook_command_unresolved" in out
    assert "1 error(s)" in out

    # Fix the dangling reference -> green.
    (target / "scripts" / "hooks" / "terminal-guard.py").write_text("pass\n")
    assert coherence_main([str(target)]) == 0
    assert "coherent" in capsys.readouterr().out
