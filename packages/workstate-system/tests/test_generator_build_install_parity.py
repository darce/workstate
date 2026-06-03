"""implementation note S2 — build⇄install generator parity (pa0020-s2-build-vs-install-generation-parity).

S2 gitignores the committed adapter copies and regenerates them from the
manifest. That is only safe if the *build* path (a hatchling build hook /
``make generate-agent-workflows`` calling the generator in-process) and the
*install* path (``workstate-bootstrap`` invoking the generator as a
subprocess) emit byte-identical trees for the same inputs — otherwise
gitignoring the committed copies trades committed-drift for build-vs-install
drift.

This pins the contract by requiring a single importable entry point
(``render_plugin_tree``) that the build path uses, and asserting its output is
byte-identical to the CLI/subprocess invocation the install path uses.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_ROOT = PACKAGE_ROOT / "workstate_system" / "payload"
GENERATOR = PAYLOAD_ROOT / "scripts" / "generate_agent_workflows.py"
MANIFEST = PAYLOAD_ROOT / "config" / "agent-workflows" / "portable_commands.json"
MCP_SERVERS_YAML = PAYLOAD_ROOT / "config" / "agent-workflows" / "mcp_servers.yaml"
SKILLS_ROOT = PAYLOAD_ROOT / "skills"


def _load_generator_module():
    spec = importlib.util.spec_from_file_location(
        "generate_agent_workflows_under_test", GENERATOR
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _relative_tree(root: Path, mapping: dict[Path, str]) -> dict[str, str]:
    return {str(path.relative_to(root)): content for path, content in mapping.items()}


def _read_tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_build_and_install_paths_emit_identical_plugin_tree(tmp_path: Path) -> None:
    """The in-process build entry point and the subprocess install path emit a
    byte-identical plugin tree for the same manifest/skills/mcp inputs."""
    mod = _load_generator_module()

    # BUILD PATH — the single shared in-process entry point a hatchling build
    # hook / `make generate-agent-workflows` calls. Returns {path: content}.
    build_out = tmp_path / "build"
    outputs = mod.render_plugin_tree(
        manifest_path=MANIFEST,
        skills_source_root=SKILLS_ROOT,
        mcp_servers_path=MCP_SERVERS_YAML,
        plugin_out=build_out,
    )
    build_tree = _relative_tree(build_out, outputs)

    # INSTALL PATH — the subprocess CLI invocation workstate-bootstrap uses.
    install_out = tmp_path / "install"
    proc = subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "--mode=plugin",
            "--manifest",
            str(MANIFEST),
            "--skills-source-root",
            str(SKILLS_ROOT),
            "--plugin-mcp-servers",
            str(MCP_SERVERS_YAML),
            "--plugin-out",
            str(install_out),
        ],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    install_tree = _read_tree(install_out)

    assert build_tree, "build path emitted no files"
    assert build_tree == install_tree


def test_render_plugin_tree_is_pure(tmp_path: Path) -> None:
    """The build entry point computes the output map without touching disk, so a
    build hook can render-then-decide (e.g. diff against committed) before any
    write lands."""
    mod = _load_generator_module()
    out = tmp_path / "plugin"
    outputs = mod.render_plugin_tree(
        manifest_path=MANIFEST,
        skills_source_root=SKILLS_ROOT,
        mcp_servers_path=MCP_SERVERS_YAML,
        plugin_out=out,
    )
    assert outputs, "expected a non-empty output map"
    # Pure: nothing was written to disk by the render call itself.
    assert not out.exists()
    # Every emitted path is rooted under the requested plugin_out.
    for path in outputs:
        assert out in path.parents
