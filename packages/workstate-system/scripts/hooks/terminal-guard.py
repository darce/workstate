#!/usr/bin/env python3
"""PreToolUse terminal guard for VS Code and Claude harnesses."""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_POLICY_VERSION = "terminal-guard-v1"
_POLICY_SOURCE = "packages/workstate-system/scripts/hooks/terminal-guard.py"
_COMMAND_PREVIEW_LIMIT = 256
_TELEMETRY_WRITE_BUDGET_SECONDS = 1.5
_SECRET_NAME_PATTERN = r"(?:token|password|passwd|secret|api[-_]?key)"
_AUTHORIZATION_RE = re.compile(r"(?i)\b(authorization:)\s+\S+")
_FLAG_EQUALS_RE = re.compile(rf"(?i)(--?(?:{_SECRET_NAME_PATTERN}))\s*=\s*([^\s]+)")
_FLAG_SPACE_RE = re.compile(rf"(?i)(--?(?:{_SECRET_NAME_PATTERN}))\s+([^\s]+)")
_ASSIGNMENT_RE = re.compile(rf"(?i)\b({_SECRET_NAME_PATTERN})\s*=\s*([^\s]+)")

# Named blocklist: explicit deterministic native-tool equivalents. Each entry
# is (rule_id, compiled pattern, native_tool, hint_template). The native_tool
# is mapped to the harness-specific hint by `_map_native_tool_hint`; when the
# mapping returns None the decision is downgraded to `ask` (see scope §1).
_BLOCKLIST: list[tuple[str, re.Pattern[str], str, str]] = [
    ("source-file-read-via-cat", re.compile(r"\bcat\s+\S"), "read_file",
     "Read file contents with `{native_tool}`, not `cat`."),
    ("source-file-read-via-sed", re.compile(r"\bsed\s+-n\b"), "read_file",
     "Read file lines with `{native_tool}`, not `sed -n`."),
    ("source-file-read-via-head",
     re.compile(r"\bhead\s+(-n\s*\d+\s+)?(?!/tmp/)\S+\.(py|ts|tsx|js|jsx|php|md|json|toml|yaml|yml|txt)\b"),
     "read_file", "Read a source file with `{native_tool}`, not `head`."),
    ("source-file-read-via-tail",
     re.compile(r"\btail\s+-n\s*\d+\s+(?!/tmp/)\S+\.(py|ts|tsx|js|jsx|php|md|json|toml|yaml|yml|txt)\b"),
     "read_file", "Read a source file with `{native_tool}`, not `tail`."),
    ("code-search-via-grep", re.compile(r"\bgrep\s+-[a-zA-Z]*[rRnliI]"), "grep_search",
     "Search code with `{native_tool}`, not `grep`."),
    ("code-search-via-rg", re.compile(r"\brg\s+"), "grep_search",
     "Search code with `{native_tool}`, not `rg`."),
    ("file-find-via-find-name", re.compile(r"\bfind\s+\S+\s+-name\b"), "file_search",
     "Find files with `{native_tool}`, not `find -name`."),
    ("git-diff-direct", re.compile(r"\bgit\s+diff\b"), "get_changed_files",
     "Inspect diffs with `{native_tool}`, not `git diff`."),
    ("git-status-direct", re.compile(r"\bgit\s+status\b"), "get_changed_files",
     "List changes with `{native_tool}`, not `git status`."),
    ("lint-mypy", re.compile(r"\bmypy\b"), "get_errors",
     "Type-check with `{native_tool}`, not `mypy`."),
    ("lint-pylint", re.compile(r"\bpylint\b"), "get_errors",
     "Lint with `{native_tool}`, not `pylint`."),
    ("lint-flake8", re.compile(r"\bflake8\b"), "get_errors",
     "Lint with `{native_tool}`, not `flake8`."),
    ("lint-ruff-check", re.compile(r"\bruff\s+check\b"), "get_errors",
     "Lint with `{native_tool}`, not `ruff check`."),
    ("lint-npm-run", re.compile(r"\bnpm\s+run\s+(lint|type-?check)\b"), "get_errors",
     "Run lint or typecheck with `{native_tool}`, not `npm run`."),
    ("lint-phpstan", re.compile(r"\bphpstan\s+analys"), "get_errors",
     "Run PHPStan with `{native_tool}`, not the CLI."),
    ("lint-phpcs", re.compile(r"\bphpcs\b"), "get_errors",
     "Run PHPCS with `{native_tool}`, not the CLI."),
    ("lint-eslint", re.compile(r"\beslint\b"), "get_errors",
     "Run ESLint with `{native_tool}`, not the CLI."),
]

# Named asklist: freeze-prone or harness-specific fallbacks that always require
# operator confirmation. Each entry is (rule_id, predicate, trigger, reason).
# `predicate` matches against the full segment (so pipeline/chained uses of
# `tee` still surface). Asklist matches are only consulted after the blocklist
# misses; see `_check_command_segment` for the precedence.
_ASKLIST: list[tuple[str, re.Pattern[str], str, str]] = [
    (
        "terminal-freeze-via-tee",
        re.compile(r"(?:^|[|;&])\s*tee\b"),
        "direct terminal output",
        (
            "Commands using `tee` are not silently allowed in this workspace because `tee` "
            "can freeze the integrated terminal.\n"
            "Run the test directly, or redirect once to /tmp/<suite>.txt and inspect it with "
            "a native read tool if capture is required."
        ),
    ),
]


def _payload_value(payload: dict, snake_key: str, camel_key: str, default: str = "") -> str:
    value = payload.get(snake_key)
    if value:
        return str(value)
    camel_value = payload.get(camel_key)
    if camel_value:
        return str(camel_value)
    return default


def _resolve_harness(tool_name: str) -> str:
    if tool_name == "run_in_terminal":
        return "vscode"
    return "claude"


def _map_native_tool_hint(harness: str, native_tool: str) -> str | None:
    if harness == "vscode":
        return native_tool
    return {
        "read_file": "Read",
        "grep_search": "Grep",
        "file_search": "Glob",
        "get_changed_files": None,
        "get_errors": None,
    }.get(native_tool)


def _normalize_command_preview(command_preview: str) -> str:
    line = (command_preview or "").splitlines()[0].strip()
    line = _AUTHORIZATION_RE.sub(r"\1 [REDACTED]", line)
    line = _FLAG_EQUALS_RE.sub(r"\1=[REDACTED]", line)
    line = _FLAG_SPACE_RE.sub(r"\1 [REDACTED]", line)
    line = _ASSIGNMENT_RE.sub(r"\1=[REDACTED]", line)
    line = " ".join(line.split())
    if len(line) <= _COMMAND_PREVIEW_LIMIT:
        return line
    return line[: _COMMAND_PREVIEW_LIMIT - 3].rstrip() + "..."


def _git_repo_root() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            root = proc.stdout.strip()
            if root:
                return root
    except Exception:
        pass
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def _resolve_agent_handoff_src(repo_root: str) -> str:
    override = os.environ.get("AGENTIC_HANDOFF_SRC_OVERRIDE")
    if override and os.path.isdir(override):
        return override

    try:
        from importlib import metadata as importlib_metadata

        dist = importlib_metadata.distribution("mcp-workstate-handoff")
        located = dist.locate_file("workstate_handoff_mcp")
        if located is not None and os.path.isdir(str(located)):
            return os.path.dirname(str(located))
    except Exception:
        pass

    overlay_src = os.path.join(
        repo_root, ".agentic", "remote", "packages", "mcp-workstate-handoff", "src"
    )
    if os.path.isdir(overlay_src):
        return overlay_src
    return os.path.join(repo_root, "packages", "mcp-workstate-handoff", "src")


def _strip_env_prefix(cmd: str) -> str:
    s = cmd.strip()
    prefix_patterns = [
        r"^setopt(?:\s+\S+)+\s*&&\s*",
        r"^cd\s+\S+\s*&&\s*",
        r"^export\s+\w+=\S+\s*&&\s*",
        r'^\w+=(?:"[^"]*"|\'[^\']*\'|\S+)\s*&&\s*',
        r'^(?:\w+=(?:"[^"]*"|\'[^\']*\'|\S+)\s+)+',
        r"^\w+=\S+\s*&&\s*",
        r"^(\w+=\S+\s+)+",
    ]
    changed = True
    while changed:
        changed = False
        for pattern in prefix_patterns:
            updated = re.sub(pattern, "", s)
            if updated != s:
                s = updated
                changed = True
                break
    return s


def _base_command(cmd: str) -> str:
    return cmd.split("|")[0].strip()


def _split_statements(cmd: str) -> list[str]:
    """Split ``cmd`` into top-level statements separated by ``&&``, ``||``, or ``;``.

    Respects single- and double-quoted regions so quoted separators do not split
    the candidate. Does not split on ``|`` (pipeline — handled by
    ``_base_command``) or a lone ``&`` (backgrounding — out of scope).

    Returns the trimmed, non-empty segments in order. A candidate with no
    top-level separator returns a single-element list containing the original
    trimmed command.
    """
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote is not None:
            current.append(ch)
            if ch == quote and (i == 0 or cmd[i - 1] != "\\"):
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            current.append(ch)
            quote = ch
            i += 1
            continue
        if cmd.startswith("&&", i) or cmd.startswith("||", i):
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            i += 2
            continue
        if ch == ";":
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        segments.append(tail)
    return segments


def _build_downgrade_reason(rule_id: str, base_command: str, native_tool: str) -> str:
    """Reason text for a blocklist rule whose native tool has no hint mapping
    under the current harness (e.g., `get_changed_files`/`get_errors` under
    Claude). The decision is downgraded from `block` to `ask` so the operator
    can confirm; the rule id is still surfaced for traceability.
    """
    if native_tool == "get_changed_files":
        category = (
            "This command duplicates changed-file inspection, but this harness has no "
            "deterministic changed-file tool hint."
        )
    elif native_tool == "get_errors":
        category = (
            "This command duplicates lint or typecheck inspection, but this harness has "
            "no deterministic diagnostic tool hint."
        )
    else:
        category = "A deterministic native-tool hint is unavailable in this harness."
    return (
        f"[terminal-guard] ASK ({rule_id}): {base_command[:80]!r}\n"
        f"{category}\n"
        "If you still need the terminal form, confirm to proceed."
    )


_DECISION_RANK = {"block": 3, "ask": 2}


def _check_command(
    command: str,
    harness: str,
) -> dict[str, str | None] | None:
    """Classify ``command`` under the default-pass policy.

    Multi-statement candidates (``A && B``, ``A || B``, ``A; B``) are split into
    top-level segments via :func:`_split_statements` and each segment is
    validated independently by :func:`_check_command_segment`. The overall
    decision is the most restrictive across segments (block > ask > pass); a
    chain whose segments all return pass returns ``None``. Closes the bypass
    where a non-matching prefix could hide a follow-on segment from
    classification.
    """
    stripped = _strip_env_prefix(command.strip())
    segments = _split_statements(stripped)
    if len(segments) <= 1:
        return _check_command_segment(stripped, harness)

    worst: dict[str, str | None] | None = None
    worst_segment: str | None = None
    for segment in segments:
        normalized = _strip_env_prefix(segment.strip())
        result = _check_command_segment(normalized, harness)
        if result is None:
            continue
        current_rank = _DECISION_RANK.get(str(result.get("decision") or ""), 0)
        worst_rank = _DECISION_RANK.get(str(worst.get("decision") or "") if worst else "", 0)
        if worst is None or current_rank > worst_rank:
            worst = result
            worst_segment = normalized
    if worst is None:
        return None

    chain_note = (
        f"[terminal-guard] CHAINED COMMAND: candidate splits into {len(segments)} "
        f"statements via `&&`/`||`/`;` and the most restrictive segment determined "
        f"the decision.\n"
        f"Failing segment: {(worst_segment or '')[:80]!r}\n"
    )
    augmented = dict(worst)
    existing_reason = worst.get("reason") or ""
    augmented["reason"] = chain_note + existing_reason
    return augmented


def _check_command_segment(
    command: str,
    harness: str,
) -> dict[str, str | None] | None:
    """Classify a single segment against the named blocklist and asklist.

    Precedence: blocklist > asklist > pass. Default is pass — only commands
    that match an explicit rule produce a non-pass decision. A blocklist rule
    whose native-tool hint is unavailable under the current harness is
    downgraded from `block` to `ask`; the rule id is still cited so the
    operator can trace the decision back to the policy source.
    """
    stripped = command.strip()
    base = _base_command(stripped)

    for rule_id, pattern, native_tool, hint_template in _BLOCKLIST:
        if not pattern.search(stripped):
            continue
        mapped_hint = _map_native_tool_hint(harness, native_tool)
        if mapped_hint is None:
            return {
                "decision": "ask",
                "trigger": rule_id,
                "native_tool_hint": None,
                "reason": _build_downgrade_reason(rule_id, base, native_tool),
            }
        return {
            "decision": "block",
            "trigger": rule_id,
            "native_tool_hint": mapped_hint,
            "reason": (
                f"[terminal-guard] BLOCKED ({rule_id}): "
                f"{hint_template.format(native_tool=mapped_hint)}\n"
                f"Prefer `{mapped_hint}` over terminal inspection here.\n"
                f"Command: {base[:80]!r}"
            ),
        }

    for rule_id, predicate, trigger, reason_body in _ASKLIST:
        if not predicate.search(stripped):
            continue
        return {
            "decision": "ask",
            "trigger": trigger,
            "native_tool_hint": None,
            "reason": (
                f"[terminal-guard] ASK ({rule_id}): {base[:80]!r}\n"
                f"{reason_body}"
            ),
        }

    return None


def _fallback_spool_path(repo_root: str) -> Path:
    return Path(repo_root) / ".task-state" / "terminal_guard.jsonl"


def _append_fallback_spool(
    repo_root: str,
    *,
    task_ref: str | None,
    worktree_path: str,
    harness: str,
    tool_name: str,
    decision: str,
    trigger: str | None,
    native_tool_hint: str | None,
    command_preview: str,
) -> None:
    try:
        spool_path = _fallback_spool_path(repo_root)
        spool_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "task_ref": task_ref,
            "worktree_path": worktree_path,
            "harness": harness,
            "tool_name": tool_name,
            "decision": decision,
            "trigger": trigger,
            "native_tool_hint": native_tool_hint,
            "command_preview": command_preview,
            "policy_version": _POLICY_VERSION,
            "policy_source": _POLICY_SOURCE,
            "fallback_source": str(spool_path),
            "created_at": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
        }
        with spool_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _record_telemetry(
    repo_root: str,
    *,
    task_ref: str | None,
    worktree_path: str,
    harness: str,
    tool_name: str,
    decision: str,
    trigger: str | None,
    native_tool_hint: str | None,
    command_preview: str,
) -> None:
    import threading

    result_container: dict[str, object] = {"ok": False}

    def _do_write() -> None:
        try:
            src_path = _resolve_agent_handoff_src(repo_root)
            if src_path not in sys.path:
                sys.path.insert(0, src_path)

            from workstate_handoff_mcp import RuntimeConfig, configure_runtime
            from workstate_handoff_mcp.terminal_telemetry import record_terminal_guard_event

            configure_runtime(RuntimeConfig.for_repo(Path(repo_root)))
            outcome = record_terminal_guard_event(
                task_ref=task_ref,
                worktree_path=worktree_path,
                harness=harness,
                tool_name=tool_name,
                decision=decision,
                trigger=trigger,
                native_tool_hint=native_tool_hint,
                command_preview=command_preview,
                policy_version=_POLICY_VERSION,
                policy_source=_POLICY_SOURCE,
            )
            result_container["ok"] = bool(outcome.get("ok"))
        except Exception:
            result_container["ok"] = False

    worker = threading.Thread(target=_do_write, daemon=True)
    worker.start()
    worker.join(timeout=_TELEMETRY_WRITE_BUDGET_SECONDS)
    if not worker.is_alive() and result_container["ok"]:
        return

    _append_fallback_spool(
        repo_root,
        task_ref=task_ref,
        worktree_path=worktree_path,
        harness=harness,
        tool_name=tool_name,
        decision=decision,
        trigger=trigger,
        native_tool_hint=native_tool_hint,
        command_preview=command_preview,
    )


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0

    try:
        from _protocol import validate_event  # type: ignore[import-not-found]

        validate_event(data, expected="PreToolUse")
    except ImportError:
        pass

    tool_name = _payload_value(data, "tool_name", "toolName")
    if tool_name not in {"run_in_terminal", "Bash"}:
        return 0

    tool_input = data.get("tool_input") or data.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        return 0
    command = _payload_value(tool_input, "command", "command")
    if not command:
        return 0

    harness = _resolve_harness(tool_name)
    repo_root = _git_repo_root()
    result = _check_command(command, harness)
    if result is None:
        return 0
    command_preview = _normalize_command_preview(command)
    task_ref = os.environ.get("AGENTIC_TASK_REF") or os.environ.get("TASK_REF") or None
    _record_telemetry(
        repo_root,
        task_ref=task_ref,
        worktree_path=repo_root,
        harness=harness,
        tool_name=tool_name,
        decision=str(result["decision"]),
        trigger=None if result["trigger"] is None else str(result["trigger"]),
        native_tool_hint=None
        if result["native_tool_hint"] is None
        else str(result["native_tool_hint"]),
        command_preview=command_preview,
    )

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": result["decision"],
                    "permissionDecisionReason": result["reason"],
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())