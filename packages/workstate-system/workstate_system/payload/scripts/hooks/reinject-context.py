#!/usr/bin/env python3
"""SessionStart hook: re-inject handoff.db references into model context.

internal (implementation note). Claude Code adds SessionStart hook
**stdout** to the model's context — the documented injection point this
repo's compaction module was missing on the read side. The hook reads the
``SessionStart`` event from stdin, gates on its ``source`` (default:
``compact`` / ``resume``; ``WORKSTATE_REINJECT_SOURCES`` overrides),
resolves the active task from the workspace, and emits ONE budgeted fenced
block of handoff.db references to stdout: task_ref, status, focus, latest
``compaction_id`` + turn range, open finding ids, the next-action hint, and
literal command hints for deeper agent-initiated recovery.

The hook is strictly read-only — it never writes handoff.db rows.

Failure-mode contract (implementation note, implementation note; mirrors compact-session.py):

- Emit the block on stdout and exit 0 on success. Diagnostics go to
  stderr only; stdout carries nothing except the injected block.
- On any gated or failed outcome (source not enabled, no active task,
  disabled compaction surface, DB unreachable, invalid settings), log a
  ``reinject skipped: <reason>`` line to stderr, emit NOTHING on stdout,
  and exit 0. A failed re-injection must never block the session start.

The single exception is strict-mode protocol drift
(``WORKSTATE_HOOK_PROTOCOL_STRICT=1`` plus a malformed event payload):
``_protocol.validate_event`` raises ``SystemExit(2)`` and the hook
propagates it, matching every other wired hook.

Tunables (documented in ``harness-protocol.yaml`` ``reinjection:`` block;
env wins over the contract default):

- ``WORKSTATE_REINJECT_SOURCES``       comma list, default ``compact,resume``
- ``WORKSTATE_REINJECT_BUDGET_CHARS``  total stdout budget, default ``1500``
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from workstate_handoff_mcp import CompactionSettings

_DEFAULT_SOURCES = ("compact", "resume")
_DEFAULT_BUDGET_CHARS = 1500
_MAX_FINDING_IDS = 5
_FENCE_OPEN = "```workstate-reinject"
_FENCE_CLOSE = "```"
_RECOVER_HINT = (
    'recover: compaction(get_latest) | get_handoff_state(read_profile="hot_summary")'
)
_HARNESS_CHOICES = ("claude-code", "codex", "grok", "cursor", "manual")


def _resolve_harness() -> str:
    raw = os.environ.get("WORKSTATE_HANDOFF_HARNESS", "").strip()
    if not raw:
        # Grok fallback (REV-E-010), mirroring compact-session.py: grok
        # delivers SessionStart hooks via the compat-loaded
        # .claude/settings.json entry, which must not carry an inline
        # WORKSTATE_HANDOFF_HARNESS export (it would mislabel Claude rows).
        # Grok exports GROK_WORKSPACE_ROOT for hook commands, so its
        # presence identifies a grok launcher when the explicit override is
        # absent; Claude Code never sets it. Without this, a grok session
        # would receive the Claude-only JSON envelope instead of the raw
        # fenced block, violating the harness-neutral injection contract
        # (implementation note R1; harness-protocol.yaml).
        if os.environ.get("GROK_WORKSPACE_ROOT", "").strip():
            return "grok"
        return "claude-code"
    if raw in _HARNESS_CHOICES:
        return raw
    return "manual"


def _emit(message: str) -> None:
    print(message, file=sys.stderr)


def _payload_value(
    payload: dict, snake_key: str, camel_key: str, default: str = ""
) -> str:
    value = payload.get(snake_key)
    if value:
        return str(value)
    camel_value = payload.get(camel_key)
    if camel_value:
        return str(camel_value)
    return default


def _git_repo_root() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:  # noqa: BLE001 -- best-effort discovery
        pass
    return os.environ.get("CLAUDE_PROJECT_DIR", "")


def _ensure_in_repo_sources_on_path(repo_root: str) -> None:
    """Make the in-repo handoff + protocol sources importable.

    Hooks run under whichever Python the harness happens to launch; pinning
    the local ``packages/.../src`` paths first guarantees the worktree's
    code handles this session start. Same contract as compact-session.py.
    """
    for relative in (
        ("packages", "workstate-protocol", "src"),
        ("packages", "mcp-workstate-handoff", "src"),
    ):
        candidate = os.path.join(repo_root, *relative)
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.insert(0, candidate)


def _enabled_sources() -> tuple[str, ...]:
    raw = os.environ.get("WORKSTATE_REINJECT_SOURCES", "")
    parsed = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    return parsed or _DEFAULT_SOURCES


def _resolve_budget_chars() -> int:
    raw = os.environ.get("WORKSTATE_REINJECT_BUDGET_CHARS", "").strip()
    if not raw:
        return _DEFAULT_BUDGET_CHARS
    budget = int(raw)  # ValueError surfaces as `invalid budget` in main()
    if budget <= 0:
        raise ValueError(f"budget must be positive, got {budget}")
    return budget


def _sanitize_field(value: str) -> str:
    """Flatten agent-authored values so they cannot break the fenced block.

    Newlines collapse to single spaces (one block line per field) and
    backtick runs of three or more shrink to two, so no interpolated value
    can ever close the ``workstate-reinject`` fence early.
    """
    flattened = " ".join(value.split())
    return re.sub(r"`{3,}", "``", flattened)


def _render_block(lines: list[str], *, budget_chars: int) -> str | None:
    """Assemble the fenced block, greedily keeping lines that fit the budget.

    The first line (task_ref) is mandatory: when the budget cannot fit the
    fences plus that line, return ``None`` so the caller skips emission
    instead of injecting a contentless fence pair. Remaining content lines
    are included in priority order while the total rendered size (including
    the trailing newline ``print`` appends) stays within ``budget_chars``.
    """

    def _rendered_len(content: list[str]) -> int:
        return len("\n".join([_FENCE_OPEN, *content, _FENCE_CLOSE])) + 1

    if not lines or _rendered_len(lines[:1]) > budget_chars:
        return None
    kept: list[str] = [lines[0]]
    for line in lines[1:]:
        if _rendered_len([*kept, line]) <= budget_chars:
            kept.append(line)
    return "\n".join([_FENCE_OPEN, *kept, _FENCE_CLOSE])


def _reinject(*, repo_root: str, budget_chars: int, settings: CompactionSettings) -> int:
    """Resolve the active task and emit the budgeted block. Read-only."""
    try:
        from workstate_handoff_mcp import (  # type: ignore[import-not-found]
            RuntimeConfig,
            configure_runtime,
            get_handoff_state,
            get_latest_compaction,
        )
        from workstate_handoff_mcp.compaction import (  # type: ignore[import-not-found]
            format_reinject_notify_message,
            format_reinject_session_start_stdout,
            reinject_json_envelope_overhead_chars,
            resolve_compaction_disabled,
        )
        from workstate_handoff_mcp.shared_primitives import (  # type: ignore[import-not-found]
            _resolve_task_ref,
        )
        from workstate_handoff_mcp.shared_schema import (  # type: ignore[import-not-found]
            _get_db_connection,
        )
    except ImportError as exc:
        _emit(f"reinject skipped: workstate_handoff_mcp import: {exc}")
        return 0

    state_dir_override = os.environ.get("WORKSTATE_HANDOFF_STATE_DIR") or None
    try:
        configure_runtime(
            RuntimeConfig.for_repo(Path(repo_root), state_dir=state_dir_override)
        )
    except Exception as exc:  # noqa: BLE001
        _emit(f"reinject skipped: runtime configuration: {exc}")
        return 0

    try:
        with _get_db_connection() as conn:
            try:
                task_ref = _resolve_task_ref(conn, None)
            except Exception as exc:  # noqa: BLE001
                _emit(f"reinject skipped: active task unresolved: {exc}")
                return 0
            # internal: a disabled compaction surface silences re-injection
            # through the same unified resolver as the Stop hook + advisory.
            disabled, disabled_source = resolve_compaction_disabled(
                env=os.environ, conn=conn, task_ref=task_ref
            )
    except Exception as exc:  # noqa: BLE001 -- DB-open failure must not crash the hook
        _emit(f"reinject skipped: resolver unreachable: {exc}")
        return 0

    if disabled:
        _emit(f"reinject skipped: disabled (source={disabled_source})")
        return 0

    try:
        envelope = get_handoff_state(task_ref=task_ref, read_profile="hot_summary")
    except Exception as exc:  # noqa: BLE001
        _emit(f"reinject skipped: handoff state read: {exc}")
        return 0
    if not envelope.get("ok"):
        _emit(f"reinject skipped: handoff state read not ok: {envelope!r:.200}")
        return 0
    data = envelope.get("data") or {}
    active = data.get("active") or {}

    try:
        latest = get_latest_compaction(task_ref)
    except Exception as exc:  # noqa: BLE001
        _emit(f"reinject skipped: latest compaction lookup: {exc}")
        return 0

    lines = [f"task_ref: {_sanitize_field(str(task_ref))}"]
    status = _sanitize_field(str(active.get("status") or ""))
    if status:
        lines.append(f"status: {status}")
    focus = _sanitize_field(str(active.get("focus") or ""))
    if focus:
        lines.append(f"focus: {focus}")
    if latest is not None:
        lines.append(
            f"latest_compaction: {latest.summary.compaction_id} "
            f"(turns {latest.summary.turn_range.start_turn}-{latest.summary.turn_range.end_turn})"
        )
    finding_ids = [
        _sanitize_field(str(row.get("finding_id") or ""))
        for row in (data.get("findings_open") or [])
        if row.get("finding_id")
    ][:_MAX_FINDING_IDS]
    if finding_ids:
        lines.append(f"open_findings: {', '.join(finding_ids)}")
    actions = data.get("actions_pending") or []
    if actions:
        next_action = _sanitize_field(str(actions[0].get("action") or ""))
        if next_action:
            lines.append(f"next_action: {next_action}")
    lines.append(_RECOVER_HINT)

    harness = _resolve_harness()
    notify_message = format_reinject_notify_message(
        task_ref=str(task_ref),
        compaction_id=latest.summary.compaction_id if latest is not None else None,
        start_turn=latest.summary.turn_range.start_turn if latest is not None else None,
        end_turn=latest.summary.turn_range.end_turn if latest is not None else None,
    )

    block_budget = budget_chars
    if harness == "claude-code" and settings.compaction_notify:
        shell_overhead = reinject_json_envelope_overhead_chars(
            block="",
            system_message=notify_message,
        )
        block_budget = max(1, budget_chars - shell_overhead)

    block = _render_block(lines, budget_chars=block_budget)
    if block is None:
        _emit(
            f"reinject skipped: budget {budget_chars} cannot fit the "
            "mandatory task_ref line"
        )
        return 0

    if harness == "claude-code" and settings.compaction_notify:
        stdout_payload = format_reinject_session_start_stdout(
            block=block,
            system_message=notify_message,
        )
        if len(stdout_payload) + 1 > budget_chars:
            shrink = len(stdout_payload) - budget_chars
            block = _render_block(lines, budget_chars=max(1, block_budget - shrink))
            if block is None:
                _emit(
                    f"reinject skipped: budget {budget_chars} cannot fit the "
                    "mandatory task_ref line"
                )
                return 0
            stdout_payload = format_reinject_session_start_stdout(
                block=block,
                system_message=notify_message,
            )
        print(stdout_payload)
        _emit(
            f"reinject emitted: task_ref={task_ref} chars={len(stdout_payload) + 1} "
            "shape=json_envelope"
        )
    else:
        print(block)
        _emit(f"reinject emitted: task_ref={task_ref} chars={len(block) + 1}")
    return 0


def main() -> int:
    repo_root = _git_repo_root()
    if not repo_root:
        _emit("reinject skipped: unable to resolve repo root")
        return 0
    _ensure_in_repo_sources_on_path(repo_root)

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        _emit("reinject skipped: malformed stdin payload")
        return 0
    if not isinstance(data, dict):
        _emit("reinject skipped: stdin payload is not an object")
        return 0

    # Cross-repo wire-shape contract: validate the SessionStart payload via
    # the shared helper. Strict mode escalates to SystemExit(2); lenient
    # mode logs and returns None. After a lenient validation failure we
    # exit 0 without injecting -- the payload cannot be trusted.
    try:
        from _protocol import validate_event  # type: ignore[import-not-found]
    except ImportError:
        validate_event = None  # type: ignore[assignment]

    if validate_event is not None:
        validated = validate_event(data, expected="SessionStart")
        if validated is None:
            _emit("reinject skipped: payload failed SessionStart schema validation")
            return 0

    # Source gate runs before any DB work so ordinary (non-enabled) session
    # starts stay cheap. Default excludes `startup` to avoid double-loading
    # next to load_session guidance.
    source = _payload_value(data, "source", "source").strip().lower()
    enabled = _enabled_sources()
    if source not in enabled:
        _emit(
            f"reinject skipped: source {source or '<unset>'!r} not enabled "
            f"(enabled: {','.join(enabled)})"
        )
        return 0

    try:
        budget_chars = _resolve_budget_chars()
    except ValueError as exc:
        _emit(f"reinject skipped: invalid budget: {exc}")
        return 0

    try:
        from workstate_handoff_mcp import CompactionSettings  # type: ignore[import-not-found]
    except ImportError as exc:
        _emit(f"reinject skipped: workstate_handoff_mcp import: {exc}")
        return 0

    try:
        settings = CompactionSettings.from_env()
    except Exception as exc:  # noqa: BLE001
        _emit(f"reinject skipped: invalid compaction settings: {exc}")
        return 0

    return _reinject(repo_root=repo_root, budget_chars=budget_chars, settings=settings)


if __name__ == "__main__":
    raise SystemExit(main())
