#!/usr/bin/env python3
"""PostToolUse hook: capture workstate-related Bash failures into agent_errors.

implementation note implementation note (WS-ERRTEL-01), Claude harness first. Reads the
PostToolUse JSON payload from stdin, pattern-matches workstate-related
failures in Bash tool results — ImportError/ModuleNotFoundError
tracebacks naming ``workstate_*`` modules, nonzero exits from workstate
make targets or CLIs, workstate MCP connection failures — classifies
them per the agent-error taxonomy, and writes through
``mcp-workstate-handoff errors-record`` so schema/DB ownership stays
with the package that defines the write contract.

Deliberately NOT captured here:
- non-workstate failures — the matcher errs toward silence on ambiguity
- MCP write rejections (ok:false envelopes) — the server self-captures
  those (implementation note); hook-side capture would double-count

Best-effort: exits 0 on any error so capture never blocks the user's
flow. ``errors-record`` itself spools when the local DB schema is
stale, so this hook never needs to reason about schema versions.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

_SUMMARY_LIMIT = 256
_DETAIL_TAIL_CHARS = 4000

_IMPORT_ERROR_RE = re.compile(
    r"(?:ImportError|ModuleNotFoundError): "
    r"(?:cannot import name '[^']+' from '(?P<from_module>[A-Za-z0-9_.]+)'"
    r"|No module named '(?P<missing_module>[A-Za-z0-9_.]+)')"
)
_WORKSTATE_COMMAND_RE = re.compile(
    r"\bmake\s+(?:task|slice|handoff|review|plan|errors|context|release)[a-z-]*\b"
    r"|\bmcp-workstate-handoff\b"
    r"|\bworkstate-bootstrap\b"
    r"|python3?\s+-m\s+workstate_[a-z_]+"
)
_WORKSTATE_TOKEN_RE = re.compile(r"workstate[_-][a-z0-9_-]+|mcp[_-]workstate[_-][a-z0-9_-]+", re.IGNORECASE)
_MCP_UNREACHABLE_RE = re.compile(
    r"MCP error -32\d\d\d|Connection (?:closed|refused|reset)|connect(?:ion)? timed? ?out",
    re.IGNORECASE,
)


def _first_matching_line(output: str, pattern: re.Pattern[str]) -> str:
    for line in output.splitlines():
        if pattern.search(line):
            return line.strip()
    return ""


def classify(*, command: str, output: str, exit_code: int) -> dict | None:
    """Classify a Bash tool result as a workstate agent error, or None.

    Silence-first: anything ambiguous or non-workstate returns None.
    Successful commands (exit 0) are never classified, even when their
    output happens to contain error-shaped text.
    """
    if exit_code == 0:
        return None
    command = command or ""
    output = output or ""

    # install_drift: ImportError/ModuleNotFoundError naming a workstate module.
    import_match = _IMPORT_ERROR_RE.search(output)
    if import_match:
        module = import_match.group("from_module") or import_match.group("missing_module") or ""
        root = module.split(".")[0]
        if root.startswith("workstate"):
            summary = _first_matching_line(output, _IMPORT_ERROR_RE) or import_match.group(0)
            return {
                "error_class": "install_drift",
                "summary": summary[:_SUMMARY_LIMIT],
                "package_name": root,
            }
        return None

    # mcp_unreachable: connection-level failure mentioning a workstate server.
    if _MCP_UNREACHABLE_RE.search(output) and (
        _WORKSTATE_TOKEN_RE.search(output) or _WORKSTATE_TOKEN_RE.search(command)
    ):
        summary = _first_matching_line(output, _MCP_UNREACHABLE_RE) or "workstate MCP server unreachable"
        return {
            "error_class": "mcp_unreachable",
            "summary": summary[:_SUMMARY_LIMIT],
        }

    # cli_failure: workstate make target / CLI exited nonzero.
    if _WORKSTATE_COMMAND_RE.search(command):
        first_error_line = ""
        for line in output.splitlines():
            stripped = line.strip()
            if stripped:
                first_error_line = stripped
                break
        summary = f"{command.strip()[:120]} exited {exit_code}"
        if first_error_line:
            summary = f"{summary}: {first_error_line}"
        return {
            "error_class": "cli_failure",
            "summary": summary[:_SUMMARY_LIMIT],
        }

    return None


def _payload_value(payload: dict, snake_key: str, camel_key: str, default: str = "") -> str:
    value = payload.get(snake_key)
    if value:
        return value
    camel_value = payload.get(camel_key)
    if camel_value:
        return camel_value
    return default


def _resolve_errors_record_argv() -> list[str]:
    """Resolve the errors-record invocation: console script, else module."""
    console_script = shutil.which("mcp-workstate-handoff")
    if console_script:
        return [console_script, "errors-record"]
    return [sys.executable, "-m", "workstate_handoff_mcp", "errors-record"]


def _resolve_agent_handoff_src(repo_root: str) -> str:
    """PYTHONPATH fallback exposing ``workstate_handoff_mcp`` (module form).

    Same resolution order as record-file-touch.py: installed
    distribution, consumer-overlay symlink, monorepo source tree.
    """
    try:
        from importlib import metadata as importlib_metadata

        dist = importlib_metadata.distribution("mcp-workstate-handoff")
        located = dist.locate_file("workstate_handoff_mcp")
        if located is not None and os.path.isdir(str(located)):
            return os.path.dirname(str(located))
    except Exception:
        pass
    overlay_src = os.path.join(repo_root, ".workstate", "remote", "packages", "mcp-workstate-handoff", "src")
    if os.path.isdir(overlay_src):
        return overlay_src
    return os.path.join(repo_root, "packages", "mcp-workstate-handoff", "src")


def process_event(data: dict) -> int:
    tool_name = _payload_value(data, "tool_name", "toolName")
    if tool_name != "Bash":
        return 0

    tool_input = data.get("tool_input") or data.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        return 0
    command = _payload_value(tool_input, "command", "command")

    tool_response = data.get("tool_response") or data.get("toolResponse") or {}
    if not isinstance(tool_response, dict):
        return 0
    stdout = str(tool_response.get("stdout") or "")
    stderr = str(tool_response.get("stderr") or "")
    exit_code_raw = tool_response.get("exitCode", tool_response.get("exit_code", 0))
    try:
        exit_code = int(exit_code_raw)
    except (TypeError, ValueError):
        exit_code = 0
    output = (stdout + "\n" + stderr).strip()

    event = classify(command=command, output=output, exit_code=exit_code)
    if event is None:
        return 0

    argv = _resolve_errors_record_argv()
    argv += ["--error-class", event["error_class"], "--summary", event["summary"]]
    if output:
        argv += ["--detail", output[-_DETAIL_TAIL_CHARS:]]
    if command:
        argv += ["--command-preview", command]
    if event.get("package_name"):
        argv += ["--package-name", event["package_name"]]
    argv += ["--tool-name", "Bash", "--harness", "claude"]

    try:
        env = os.environ.copy()
        repo_root = env.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        src_path = _resolve_agent_handoff_src(repo_root)
        env["PYTHONPATH"] = src_path + (os.pathsep + env.get("PYTHONPATH", ""))
        subprocess.run(argv, capture_output=True, timeout=10, env=env)
    except Exception:
        pass
    return 0


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0

    try:
        from _protocol import validate_event  # type: ignore[import-not-found]

        validate_event(data, expected="PostToolUse")
    except ImportError:
        pass

    try:
        return process_event(data)
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
