"""Devious harness fakes (implementation note S7 / Nygard Test Harness)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def write_hanging_git_fake(path: Path) -> Path:
    script = path / "git"
    script.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  -C) shift ;;\n"
        "esac\n"
        "case \"$1\" in\n"
        "  clone|fetch) sleep 60 ;;\n"
        "  *) exec /usr/bin/env git \"$@\" ;;\n"
        "esac\n"
    )
    script.chmod(0o755)
    return script


def write_half_write_generator(path: Path) -> Path:
    script = path / "generate_agent_workflows.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "target = None\n"
        "args = sys.argv[1:]\n"
        "for i, arg in enumerate(args):\n"
        "    if arg == '--target' and i + 1 < len(args):\n"
        "        target = pathlib.Path(args[i + 1])\n"
        "if target:\n"
        "    (target / '.github' / 'prompts').mkdir(parents=True, exist_ok=True)\n"
        "    (target / '.github' / 'prompts' / 'partial.prompt.md').write_text('partial')\n"
        "sys.exit(1)\n"
    )
    script.chmod(0o755)
    return script


def write_grok_timeout_fake(path: Path) -> Path:
    script = path / "grok"
    script.write_text("#!/bin/sh\nsleep 60\n")
    script.chmod(0o755)
    return script


def write_garbage_generator(path: Path) -> Path:
    script = path / "generate_agent_workflows.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stderr.write('GARBAGE_OUTPUT_NOT_JSON\\n')\n"
        "sys.exit(2)\n"
    )
    script.chmod(0o755)
    return script


def write_offline_uv_fake(path: Path) -> Path:
    script = path / "uv"
    script.write_text(
        "#!/bin/sh\n"
        "echo 'offline uv fake' >&2\n"
        "exit 1\n"
    )
    script.chmod(0o755)
    uvx = path / "uvx"
    uvx.write_text(
        "#!/bin/sh\n"
        "echo 'offline uvx fake' >&2\n"
        "exit 1\n"
    )
    uvx.chmod(0o755)
    return script


def write_hanging_uvx_fake(path: Path) -> Path:
    uvx = path / "uvx"
    uvx.write_text("#!/bin/sh\nsleep 60\n")
    uvx.chmod(0o755)
    return uvx


def prepend_path(env: dict[str, str], directory: Path) -> dict[str, str]:
    merged = dict(env)
    merged["PATH"] = f"{directory}{os.pathsep}{merged.get('PATH', '')}"
    return merged