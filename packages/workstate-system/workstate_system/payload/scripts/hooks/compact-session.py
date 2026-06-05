#!/usr/bin/env python3
"""Stop hook: persist a structured session_compactions row at turn-end.

WORKSTATE-REF-34 implementation note (Option B). Reads the harness ``Stop`` event from
stdin, derives the active task from the workspace, and calls
``workstate_handoff_mcp.compact_session(...)`` against the transcript the
harness just closed. The new ``compaction_id`` is surfaced as the first
stderr line in a ``compaction_id=C-...`` envelope so the harness can keep
it in its retained summary; receipt value fields follow as stable
``key=value`` lines for operators that want the compression delta.

Failure-mode contract (must match implementation note of the WORKSTATE-REF-34 task plan):

- Exit 0 on success after writing ``compaction_id=`` as the first stderr
    line, followed by receipt value ``key=value`` lines.
- Exit 0 also when there is nothing new to compact -- transcript head
  matches the latest stored compaction's ``turn_range``. Logs a
  ``compaction skipped: <reason>`` line and exits cleanly so the
  harness turn is not blocked.
- On any internal error (DB unreachable, transcript missing, no active
  task, validation failure), log a ``compaction failed: <reason>`` line
  and exit 0 *without* writing ``compaction_id=``. A failed compaction
  must never block the harness turn.

The single exception is strict-mode protocol drift
(``WORKSTATE_HOOK_PROTOCOL_STRICT=1`` plus a malformed event payload):
``_protocol.validate_event`` raises ``SystemExit(2)`` and the hook
propagates it, matching every other wired hook.
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

_TURN_NUMBER_RE = re.compile(r"\bturn\s+(\d+)\b", re.IGNORECASE)


def _slice_new_turn_text(transcript: str, *, since_turn: int) -> str:
    """Return transcript text covering turns strictly after ``since_turn``.

    When ``since_turn`` is 0 (no prior compaction in this session) the
    entire transcript is treated as new.
    """
    if since_turn <= 0:
        return transcript
    out: list[str] = []
    keeping = False
    for line in transcript.splitlines(keepends=True):
        match = _TURN_NUMBER_RE.search(line)
        if match and int(match.group(1)) > since_turn:
            keeping = True
        if keeping:
            out.append(line)
    return "".join(out)


def _count_new_turn_tokens(text: str) -> int:
    """Encode ``text`` with ``cl100k_base`` and return token count.

    Falls back to whitespace word count when tiktoken is unavailable so
    the threshold gate is conservative (never silently skip a real
    compaction because the encoder import failed).
    """
    try:
        import tiktoken  # type: ignore[import-not-found]

        encoder = tiktoken.get_encoding("cl100k_base")
        return len(encoder.encode(text))
    except Exception:  # noqa: BLE001
        return len(text.split())


def _payload_value(payload: dict, snake_key: str, camel_key: str, default: str = "") -> str:
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

    Hooks run under whichever Python the harness happens to launch.
    A globally-installed ``workstate_handoff_mcp`` may exist but point at
    a different worktree; pinning the local ``packages/.../src`` paths
    first guarantees the worktree's code is what handles this turn's
    compaction.
    """
    for relative in (
        ("packages", "workstate-protocol", "src"),
        ("packages", "mcp-workstate-handoff", "src"),
    ):
        candidate = os.path.join(repo_root, *relative)
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.insert(0, candidate)


def _emit(message: str) -> None:
    print(message, file=sys.stderr)


_HARNESS_CHOICES = ("claude-code", "codex", "cursor", "manual")


def _resolve_harness() -> str:
    """Derive the harness label from WORKSTATE_HANDOFF_HARNESS.

    The Stop hook is wired into multiple harnesses (Claude Code, Codex,
    Cursor, manual scripted runs); each launcher exports
    ``WORKSTATE_HANDOFF_HARNESS`` so the compaction row reflects the actual
    caller. Unknown values are coerced to ``"manual"`` rather than
    rejected — a wrong-but-recognized label would silently mislabel
    rows. Defaults to ``"claude-code"`` only when the env is unset, to
    preserve the historical default for the harness this hook first
    shipped under. See WORKSTATE-REF-34-BR-20260501-02.
    """
    raw = os.environ.get("WORKSTATE_HANDOFF_HARNESS", "").strip()
    if not raw:
        return "claude-code"
    if raw in _HARNESS_CHOICES:
        return raw
    return "manual"


def _compact(
    *,
    transcript_path: str,
    session_id: str,
    repo_root: str,
    settings: CompactionSettings,
) -> int:
    """Drive ``compact_session`` against the resolved active task.

    Returns process exit code (always 0 by contract). ``settings`` is
    built once in ``main()``; this function never re-reads compaction env
    vars.
    """
    try:
        from workstate_handoff_mcp import (  # type: ignore[import-not-found]
            RuntimeConfig,
            configure_runtime,
            compact_session,
            get_latest_compaction,
        )
        from workstate_handoff_mcp.compaction import (  # type: ignore[import-not-found]
            _derive_turn_range,
            _read_transcript,
            format_compaction_record_receipt_lines,
        )
        from workstate_handoff_mcp.shared_primitives import (  # type: ignore[import-not-found]
            _resolve_task_ref,
        )
        from workstate_handoff_mcp.shared_schema import (  # type: ignore[import-not-found]
            _get_db_connection,
        )
    except ImportError as exc:
        _emit(f"compaction failed: workstate_handoff_mcp import: {exc}")
        return 0

    # Honor WORKSTATE_HANDOFF_STATE_DIR to mirror the MCP server CLI's
    # from_args resolution; production callers leave it unset and the
    # primary worktree's .task-state wins.
    state_dir_override = os.environ.get("WORKSTATE_HANDOFF_STATE_DIR") or None
    try:
        configure_runtime(
            RuntimeConfig.for_repo(Path(repo_root), state_dir=state_dir_override)
        )
    except Exception as exc:  # noqa: BLE001
        _emit(f"compaction failed: runtime configuration: {exc}")
        return 0

    try:
        transcript = _read_transcript(transcript_path)
    except Exception as exc:  # noqa: BLE001
        _emit(f"compaction failed: transcript unreadable: {exc}")
        return 0

    try:
        with _get_db_connection() as conn:
            task_ref = _resolve_task_ref(conn, None)
    except Exception as exc:  # noqa: BLE001
        _emit(f"compaction failed: active task unresolved: {exc}")
        return 0

    try:
        latest = get_latest_compaction(task_ref)
    except Exception as exc:  # noqa: BLE001
        _emit(f"compaction failed: latest compaction lookup: {exc}")
        return 0

    try:
        current_range = _derive_turn_range(transcript)
    except Exception as exc:  # noqa: BLE001
        _emit(f"compaction failed: turn range derivation: {exc}")
        return 0

    if latest is not None and latest.session_id == session_id:
        # Only short-circuit when the *same* harness session is firing
        # the Stop hook again with no new turns. Different session_ids
        # (resumed sessions, new sessions, cross-harness handoffs) reset
        # transcript-local turn numbering, so a `current.end_turn <=
        # latest.end_turn` comparison would silently drop their
        # compaction. See WORKSTATE-REF-34-BR-20260501-01.
        if current_range.end_turn <= latest.turn_range.end_turn:
            _emit(
                "compaction skipped: no new turns since "
                f"{latest.compaction_id}"
            )
            return 0
        prior_end_turn = latest.turn_range.end_turn
    else:
        prior_end_turn = 0

    new_turn_count = current_range.end_turn - prior_end_turn
    if new_turn_count < settings.min_new_turns:
        _emit(
            f"compaction skipped: only {new_turn_count} new turn(s); "
            f"threshold {settings.min_new_turns}"
        )
        return 0

    if settings.min_new_tokens > 0:
        new_text = _slice_new_turn_text(transcript, since_turn=prior_end_turn)
        new_token_count = _count_new_turn_tokens(new_text)
        if new_token_count < settings.min_new_tokens:
            _emit(
                f"compaction skipped: only {new_token_count} new tokens; "
                f"threshold {settings.min_new_tokens}"
            )
            return 0

    try:
        receipt = compact_session(
            transcript_path=transcript_path,
            task_ref=task_ref,
            harness=_resolve_harness(),
            session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001
        _emit(f"compaction failed: compact_session: {exc}")
        return 0

    for line in format_compaction_record_receipt_lines(receipt):
        _emit(line)
    return 0


def main() -> int:
    repo_root = _git_repo_root()
    if not repo_root:
        _emit("compaction failed: unable to resolve repo root")
        return 0
    _ensure_in_repo_sources_on_path(repo_root)

    # Single typed boundary for compaction env vars. Building settings
    # here (not inside ``_compact``) validates the WORKSTATE_HANDOFF_COMPACTION_*
    # env vars once, so a bad value surfaces as one ``compaction failed`` line.
    try:
        from workstate_handoff_mcp import CompactionSettings  # type: ignore[import-not-found]
    except ImportError as exc:
        _emit(f"compaction failed: workstate_handoff_mcp import: {exc}")
        return 0

    try:
        settings = CompactionSettings.from_env()
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError or env coercion
        _emit(f"compaction failed: invalid compaction settings: {exc}")
        return 0

    # WORKSTATE-REF-67: route both the Stop hook and the advisory through the
    # unified resolver so a single operator action turns off both surfaces.
    # The resolver consults env first, then a task-scoped
    # ``compaction_settings`` row, then the workspace-default row. Resolve
    # the active task before the check so per-task DB disables silence the
    # Stop hook as well as the advisory.
    try:
        from workstate_handoff_mcp import (  # type: ignore[import-not-found]
            RuntimeConfig,
            configure_runtime,
        )
        from workstate_handoff_mcp.compaction import (  # type: ignore[import-not-found]
            resolve_compaction_disabled,
        )
        from workstate_handoff_mcp.shared_primitives import (  # type: ignore[import-not-found]
            _resolve_task_ref,
        )
        from workstate_handoff_mcp.shared_schema import (  # type: ignore[import-not-found]
            _get_db_connection,
        )
    except ImportError as exc:
        _emit(f"compaction failed: workstate_handoff_mcp resolver import: {exc}")
        return 0

    state_dir_override = os.environ.get("WORKSTATE_HANDOFF_STATE_DIR") or None
    try:
        configure_runtime(
            RuntimeConfig.for_repo(Path(repo_root), state_dir=state_dir_override)
        )
    except Exception as exc:  # noqa: BLE001
        _emit(f"compaction failed: runtime configuration: {exc}")
        return 0

    try:
        with _get_db_connection() as conn:
            try:
                resolved_task_ref = _resolve_task_ref(conn, None)
            except Exception:  # noqa: BLE001 -- no active task falls through to workspace/env checks
                resolved_task_ref = None
            disabled, disabled_source = resolve_compaction_disabled(
                env=os.environ, conn=conn, task_ref=resolved_task_ref
            )
    except Exception as exc:  # noqa: BLE001 -- DB-open failure must not crash the hook
        _emit(f"compaction failed: resolver unreachable: {exc}")
        return 0

    if disabled:
        _emit(f"compaction skipped: disabled (source={disabled_source})")
        return 0

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        _emit("compaction skipped: malformed stdin payload")
        return 0
    if not isinstance(data, dict):
        _emit("compaction skipped: stdin payload is not an object")
        return 0

    # Cross-repo wire-shape contract: validate the Stop payload via the
    # shared helper. Strict mode escalates to SystemExit(2); lenient
    # mode logs and returns None. After a lenient validation failure we
    # exit 0 without compacting -- the payload cannot be trusted.
    try:
        from _protocol import validate_event  # type: ignore[import-not-found]
    except ImportError:
        validate_event = None  # type: ignore[assignment]

    if validate_event is not None:
        validated = validate_event(data, expected="Stop")
        if validated is None:
            # Lenient mode swallowed a validation error; do not compact.
            # Strict mode would already have raised SystemExit(2).
            _emit("compaction skipped: payload failed Stop schema validation")
            return 0

    transcript_path = _payload_value(data, "transcript_path", "transcriptPath")
    session_id = _payload_value(data, "session_id", "sessionId")

    if not session_id:
        _emit("compaction skipped: missing session_id")
        return 0
    if not transcript_path:
        _emit("compaction skipped: missing transcript_path")
        return 0

    return _compact(
        transcript_path=transcript_path,
        session_id=session_id,
        repo_root=repo_root,
        settings=settings,
    )


if __name__ == "__main__":
    raise SystemExit(main())
