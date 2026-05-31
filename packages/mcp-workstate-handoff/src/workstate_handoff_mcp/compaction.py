"""Compaction helpers.

WORKSTATE-REF's compaction surfaces (Stop hook, advisory, ``session_compactions``
rows) run alongside the host harness's built-in context compaction — they
are not a replacement for it. The two mechanisms solve different problems
(in-conversation context pressure vs. durable cross-session retained
context). See
``packages/mcp-workstate-handoff/docs/explainers/compaction-vs-default-harness-compaction.md``
for the operator-facing comparison and the explicit note on what the
``test_compression_ratio.py`` benchmark actually measures.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from json import JSONDecodeError
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field, field_validator
from workstate_protocol import StructuredSummary, TurnRange

from .compaction_contract import (
    CompactionContract,
    CompactionContractHarness,
    TranscriptDiscoveryRule,
    build_contract_source_report,
    detect_active_harness,
    load_compaction_contract,
    normalize_compaction_harness,
    resolve_effective_thresholds,
)
from .review_findings import list_review_findings
from .shared_primitives import _resolve_task_ref
from .shared_schema import _get_db_connection
from .touched_files import get_touched_files
from .verified_tests import get_verified_tests

_log = logging.getLogger("workstate_handoff_mcp")

CompactionHarness = Literal["claude-code", "codex", "vscode", "manual"]
CompactionHarnessInput = Literal["claude-code", "codex", "vscode", "manual", "cursor"]
COMPACTION_HARNESS_CHOICES: tuple[str, ...] = ("claude-code", "codex", "vscode", "manual")
COMPACTION_HARNESS_INPUT_CHOICES: tuple[str, ...] = (
    "claude-code",
    "codex",
    "vscode",
    "manual",
    "cursor",
)
PROSE_RESIDUAL_SOFT_LIMIT_CHARS = 4096
PROSE_RESIDUAL_HARD_LIMIT_CHARS = 16384
TRANSCRIPT_PRE_READ_HARD_LIMIT_BYTES = PROSE_RESIDUAL_HARD_LIMIT_CHARS * 10
TURN_NUMBER_RE = re.compile(r"\bturn\s+(\d+)\b", re.IGNORECASE)

DEFAULT_MIN_NEW_TURNS = 1
DEFAULT_MIN_NEW_TOKENS = 0

_COMPACTION_ENV_FIELDS: tuple[tuple[str, str], ...] = (
    (
        "disabled",
        "WORKSTATE_HANDOFF_COMPACTION_DISABLED",
    ),
    (
        "min_new_turns",
        "WORKSTATE_HANDOFF_COMPACTION_MIN_NEW_TURNS",
    ),
    (
        "min_new_tokens",
        "WORKSTATE_HANDOFF_COMPACTION_MIN_NEW_TOKENS",
    ),
)

_FALSY_DISABLED_VALUES: frozenset[str] = frozenset({"", "0", "false", "no", "off"})


def _coerce_disabled(value: str) -> bool:
    return value.strip().lower() not in _FALSY_DISABLED_VALUES


def _env_disables_compaction(env: Mapping[str, str]) -> bool:
    raw = env.get("WORKSTATE_HANDOFF_COMPACTION_DISABLED", "")
    return raw != "" and _coerce_disabled(raw)


class CompactionSettings(BaseModel):
    """Typed config surface for the compaction hook and library.

    Reads ``WORKSTATE_HANDOFF_COMPACTION_*`` env vars at the boundary;
    Bad values raise ``pydantic.ValidationError`` so typos become loud failures
    rather than silent default fallbacks.
    """

    disabled: bool = False
    min_new_turns: int = Field(default=DEFAULT_MIN_NEW_TURNS, ge=0)
    min_new_tokens: int = Field(default=DEFAULT_MIN_NEW_TOKENS, ge=0)

    @field_validator("min_new_turns", "min_new_tokens", mode="before")
    @classmethod
    def _strip_int_strings(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CompactionSettings":
        source = env if env is not None else os.environ
        fields: dict[str, object] = {}
        for field_name, canonical in _COMPACTION_ENV_FIELDS:
            raw = source.get(canonical, "")
            if raw == "":
                continue
            if field_name == "disabled":
                fields[field_name] = _coerce_disabled(raw)
            else:
                fields[field_name] = raw
        return cls.model_validate(fields)


DisabledSource = Literal["env", "db"]


class CompactionDisabledRow(BaseModel):
    """Row from ``compaction_settings`` exposed to status callers."""

    scope_kind: Literal["task", "workspace"]
    task_ref: str | None
    enabled: bool
    updated_at: str
    updated_by: str | None


class CompactionStatusReceipt(BaseModel):
    """Return shape for ``compaction(operation='status', task_ref=...)``.

    ``env_override`` is ``True`` when the env var is forcing the disabled
    state regardless of any db row, mirroring WORKSTATE-REF-63's ``thresholds_source``
    disclosure pattern.
    """

    disabled: bool
    source: DisabledSource | None
    env_override: bool
    db_row: CompactionDisabledRow | None


class CompactionRecordReceipt(BaseModel):
    """Return shape for ``compaction(operation='record', ...)`` (WORKSTATE-REF-67).

    The receipt **inlines** the canonical ``StructuredSummary`` rather than
    duplicating its counts (WORKSTATE-REF-004 fix). Callers read counts as
    ``len(receipt.summary.decisions)``, ``len(receipt.summary.files_touched)``,
    etc.

    ``tokens_saved_estimate`` reuses the ``chars / 4`` divisor documented at
    ``packages/workstate-system/docs/workstate/contracts/harness-protocol.yaml``
    lines 126-127 (WORKSTATE-REF-007 fix) — the same fallback estimator the threshold
    layer uses (``70_000 * 4 = 280_000``). The estimate is clamped
    non-negative so a summary that ends up larger than the input transcript
    does not surface a negative number.

    ``db_row_id`` is the ``lastrowid`` captured from the ``INSERT`` cursor on
    the ``session_compactions`` table — useful for cross-referencing the
    receipt against the persisted row in tests and reviews.
    """

    compaction_id: str
    summary: StructuredSummary
    input_chars: int
    summary_chars: int
    prose_residual_chars: int
    tokens_saved_estimate: int
    db_row_id: int


class CompactionAdvisoryThresholds(BaseModel):
    """Integer-valued threshold pair used for both ``thresholds`` and ``observed``."""

    tokens: int | None = None
    chars: int | None = None


class CompactionAdvisoryThresholdSources(BaseModel):
    """String-valued provenance pair for ``thresholds_source`` (enabled branch only)."""

    tokens: str | None = None
    chars: str | None = None


class CompactionAdvisoryTranscript(BaseModel):
    """Transcript locator pair returned under ``transcript``."""

    path: str | None = None
    source: str | None = None


class CompactionAdvisory(BaseModel):
    """Return shape for ``compute_compaction_advisory`` (WORKSTATE-REF-61 envelope).

    Callers historically consumed the dict form; ``compute_compaction_advisory``
    serialises with ``.model_dump(mode='json')`` to preserve that contract.
    """

    recommended: bool = False
    recommended_action: str | None = None
    thresholds: CompactionAdvisoryThresholds = Field(default_factory=CompactionAdvisoryThresholds)
    thresholds_source: CompactionAdvisoryThresholdSources | None = None
    observed: CompactionAdvisoryThresholds = Field(default_factory=CompactionAdvisoryThresholds)
    harness: str | None = None
    transcript: CompactionAdvisoryTranscript = Field(default_factory=CompactionAdvisoryTranscript)
    contract_source: dict | None = None
    latest_compaction_id: str | None = None
    disabled: bool = False
    disabled_source: DisabledSource | None = None
    warnings: list[str] = Field(default_factory=list)


COMPACTION_RECORD_RECEIPT_OPERATOR_FIELDS: tuple[str, ...] = (
    "tokens_saved_estimate",
    "input_chars",
    "summary_chars",
    "prose_residual_chars",
)


def format_compaction_record_receipt_lines(receipt: CompactionRecordReceipt) -> list[str]:
    """Return the stable operator-facing receipt lines for a recorded compaction."""
    lines = [f"compaction_id={receipt.compaction_id}"]
    for field in COMPACTION_RECORD_RECEIPT_OPERATOR_FIELDS:
        lines.append(f"{field}={getattr(receipt, field)}")
    return lines


def _ensure_compaction_settings_table(conn: sqlite3.Connection) -> None:
    """Create ``compaction_settings`` if it does not exist.

    The HANDOFF_SCHEMA_SQL bootstrap and the warm-start migration both
    create this table, but disable/enable/status callers may be invoked
    before the warm-start migration when running against very old DBs.
    This helper makes the runtime write-path tolerant.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS compaction_settings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_kind  TEXT NOT NULL CHECK (scope_kind IN ('task', 'workspace')),
            task_ref    TEXT,
            enabled     INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_by  TEXT
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_compaction_settings_scope "
        "ON compaction_settings(scope_kind, COALESCE(task_ref, ''))"
    )


def get_compaction_disabled_row(
    conn: sqlite3.Connection,
    *,
    scope_kind: Literal["task", "workspace"],
    task_ref: str | None,
) -> CompactionDisabledRow | None:
    """Return the matching compaction_settings row, or ``None`` if absent."""

    _ensure_compaction_settings_table(conn)
    if scope_kind == "workspace":
        row = conn.execute(
            "SELECT scope_kind, task_ref, enabled, updated_at, updated_by "
            "FROM compaction_settings WHERE scope_kind = 'workspace'"
        ).fetchone()
    else:
        if task_ref is None:
            return None
        row = conn.execute(
            "SELECT scope_kind, task_ref, enabled, updated_at, updated_by "
            "FROM compaction_settings WHERE scope_kind = 'task' AND task_ref = ?",
            (task_ref,),
        ).fetchone()
    if row is None:
        return None
    return CompactionDisabledRow(
        scope_kind=str(row["scope_kind"]),  # type: ignore[arg-type]
        task_ref=row["task_ref"] if row["task_ref"] is not None else None,
        enabled=bool(int(row["enabled"])),
        updated_at=str(row["updated_at"]),
        updated_by=str(row["updated_by"]) if row["updated_by"] is not None else None,
    )


def upsert_compaction_disabled(
    conn: sqlite3.Connection,
    *,
    scope_kind: Literal["task", "workspace"],
    task_ref: str | None,
    enabled: bool,
    actor: str | None = None,
) -> CompactionDisabledRow:
    """Upsert a compaction_settings row keyed by (scope_kind, task_ref).

    Uses ``ON CONFLICT (scope_kind, COALESCE(task_ref,'')) DO UPDATE``
    so the workspace-default row is a singleton and repeated task-scoped
    writes for the same task_ref refresh in place.
    """

    _ensure_compaction_settings_table(conn)
    if scope_kind == "task" and not task_ref:
        raise ValueError("task_ref is required when scope_kind='task'")
    if scope_kind == "workspace":
        task_ref = None
    now_iso = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO compaction_settings (scope_kind, task_ref, enabled, updated_at, updated_by)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (scope_kind, COALESCE(task_ref, '')) DO UPDATE SET
            enabled = excluded.enabled,
            updated_at = excluded.updated_at,
            updated_by = excluded.updated_by
        """,
        (scope_kind, task_ref, 1 if enabled else 0, now_iso, actor),
    )
    refreshed = get_compaction_disabled_row(conn, scope_kind=scope_kind, task_ref=task_ref)
    if refreshed is None:  # pragma: no cover -- defensive
        raise RuntimeError("compaction_settings upsert returned no row")
    return refreshed


def resolve_compaction_disabled(
    *,
    env: Mapping[str, str],
    conn: sqlite3.Connection,
    task_ref: str | None,
) -> tuple[bool, DisabledSource | None]:
    """Return ``(disabled, source)`` for the unified WORKSTATE-REF disable surface.

    Precedence (first match wins):
    1. ``WORKSTATE_HANDOFF_COMPACTION_DISABLED`` env var.
      2. ``compaction_settings`` row with ``scope_kind='task'`` and a
         matching ``task_ref`` and ``enabled=0``.
      3. ``compaction_settings`` row with ``scope_kind='workspace'`` and
         ``enabled=0``.
      4. Otherwise ``(False, None)``.

    The caller is expected to pass its existing SQLite connection so
    that disable lookups do not double-open the DB (WORKSTATE67-PLAN WORKSTATE-REF-009).
    """

    if _env_disables_compaction(env):
        return True, "env"

    _ensure_compaction_settings_table(conn)
    if task_ref:
        task_row = get_compaction_disabled_row(conn, scope_kind="task", task_ref=task_ref)
        if task_row is not None and not task_row.enabled:
            return True, "db"
    workspace_row = get_compaction_disabled_row(conn, scope_kind="workspace", task_ref=None)
    if workspace_row is not None and not workspace_row.enabled:
        return True, "db"
    return False, None


def compute_compaction_status(
    *,
    env: Mapping[str, str],
    conn: sqlite3.Connection,
    task_ref: str | None,
) -> CompactionStatusReceipt:
    """Return the structured ``CompactionStatusReceipt`` envelope."""

    env_override = _env_disables_compaction(env)

    db_row: CompactionDisabledRow | None = None
    if task_ref:
        db_row = get_compaction_disabled_row(conn, scope_kind="task", task_ref=task_ref)
    if db_row is None:
        db_row = get_compaction_disabled_row(conn, scope_kind="workspace", task_ref=None)

    disabled, source = resolve_compaction_disabled(env=env, conn=conn, task_ref=task_ref)
    return CompactionStatusReceipt(
        disabled=disabled,
        source=source,
        env_override=env_override,
        db_row=db_row,
    )


def set_compaction_enabled(
    *,
    enabled: bool,
    task_ref: str | None,
    actor: str | None = None,
) -> CompactionStatusReceipt:
    """Top-level helper for the MCP/CLI disable/enable ops.

    Opens its own short-lived connection. Returns the resolved status
    receipt so callers can echo the post-write state back to the user.
    """

    scope_kind: Literal["task", "workspace"] = "task" if task_ref else "workspace"
    with _get_db_connection() as conn:
        upsert_compaction_disabled(
            conn,
            scope_kind=scope_kind,
            task_ref=task_ref,
            enabled=enabled,
            actor=actor,
        )
        conn.commit()
        return compute_compaction_status(env=os.environ, conn=conn, task_ref=task_ref)


def get_compaction_status(task_ref: str | None) -> CompactionStatusReceipt:
    """Top-level helper for the ``status`` MCP op."""

    with _get_db_connection() as conn:
        return compute_compaction_status(env=os.environ, conn=conn, task_ref=task_ref)


def _parse_changed_files_json(raw_value: object) -> list[str]:
    if not isinstance(raw_value, str) or raw_value.strip() == "":
        return []
    try:
        decoded = json.loads(raw_value)
    except JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded if isinstance(item, str) and item]


class _CompactionQueries:
    """Per-task query bundle binding ``(conn, task_ref)`` for compaction helpers.

    Fowler "Combine Functions into Class" (p. 144): the four primitive
    helpers below all carried the same ``(conn, task_ref)`` clump. Binding
    it once at construction removes the repeated parameters at every call
    site and groups related SQL by domain.
    """

    def __init__(self, conn: sqlite3.Connection, task_ref: str) -> None:
        self._conn = conn
        self._task_ref = task_ref

    def load_task_decisions(self) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT decision, changed_files_json FROM decisions WHERE task_ref = ? ORDER BY created_at ASC, id ASC",
            (self._task_ref,),
        ).fetchall()
        decisions: list[dict[str, object]] = []
        for row in rows:
            decisions.append(
                {
                    "decision_id": str(row["decision"]),
                    "slug": str(row["decision"]),
                    "changed_files": _parse_changed_files_json(row["changed_files_json"]),
                }
            )
        return decisions

    def next_compaction_id(self) -> str:
        # MAX(compaction_id) is safe because the suffix is fixed-width zero-padded;
        # if that invariant changes, this query must change with it.
        row = self._conn.execute(
            "SELECT MAX(compaction_id) AS compaction_id FROM session_compactions WHERE task_ref = ?",
            (self._task_ref,),
        ).fetchone()
        if row is None or row["compaction_id"] is None:
            next_suffix = 1
        else:
            raw_compaction_id = str(row["compaction_id"])
            suffix_text = raw_compaction_id.rsplit("-", 1)[-1]
            try:
                next_suffix = int(suffix_text) + 1
            except ValueError as exc:
                raise ValueError(
                    f"Malformed compaction_id stored for task {self._task_ref}: {raw_compaction_id}"
                ) from exc
        if next_suffix > 9999:
            raise ValueError(f"compaction_id suffix overflow for task {self._task_ref}")
        return f"C-{self._task_ref}-{next_suffix:04d}"

    def count_decisions_after(self, after: datetime) -> int:
        # Stored compaction created_at is ISO with 'T'/'Z'; decisions.created_at is
        # the SQLite default 'YYYY-MM-DD HH:MM:SS'. Compare as ISO strings after
        # normalizing the compaction value to the same shape.
        cutoff = after.strftime("%Y-%m-%d %H:%M:%S")
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM decisions WHERE task_ref = ? AND created_at > ?",
            (self._task_ref, cutoff),
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def observed_token_total(self, since: datetime | None) -> int | None:
        if since is None:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) AS s FROM turn_metrics WHERE task_ref = ?",
                (self._task_ref,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) AS s FROM turn_metrics WHERE task_ref = ? AND created_at > ?",
                (self._task_ref, since.strftime("%Y-%m-%d %H:%M:%S")),
            ).fetchone()
        if row is None:
            return None
        return int(row["s"] or 0)


def _ordered_unique(items: list[str]) -> list[str]:
    return list(OrderedDict.fromkeys(items))


def _validate_harness(harness: str) -> CompactionHarness:
    return normalize_compaction_harness(harness)


def _derive_turn_range(transcript: str) -> TurnRange:
    turn_numbers = [int(match.group(1)) for match in TURN_NUMBER_RE.finditer(transcript)]
    if turn_numbers:
        return TurnRange(start_turn=min(turn_numbers), end_turn=max(turn_numbers))
    non_empty_lines = [line for line in transcript.splitlines() if line.strip()]
    line_count = max(1, len(non_empty_lines))
    return TurnRange(start_turn=1, end_turn=line_count)


def _split_transcript_lines(transcript: str) -> list[str]:
    return [line.strip() for line in transcript.splitlines() if line.strip()]


def _truncate_residual(residual: str | None) -> str | None:
    if residual is None:
        return None
    if len(residual) > PROSE_RESIDUAL_HARD_LIMIT_CHARS:
        raise ValueError(f"prose_residual exceeds hard limit of {PROSE_RESIDUAL_HARD_LIMIT_CHARS} chars")
    if len(residual) <= PROSE_RESIDUAL_SOFT_LIMIT_CHARS:
        return residual
    omitted = len(residual) - PROSE_RESIDUAL_SOFT_LIMIT_CHARS
    _log.warning("Compaction prose_residual exceeded soft limit; truncating %s chars", omitted)
    return residual[:PROSE_RESIDUAL_SOFT_LIMIT_CHARS] + f"... [truncated; {omitted} chars omitted]"


def _read_transcript(transcript_path: str | Path) -> str:
    path = Path(transcript_path)
    if path.stat().st_size > TRANSCRIPT_PRE_READ_HARD_LIMIT_BYTES:
        raise ValueError(f"Transcript exceeds pre-read hard limit of {TRANSCRIPT_PRE_READ_HARD_LIMIT_BYTES} bytes")
    return path.read_text()


def _collect_finding_matches(
    transcript_lines: list[str],
    finding_rows: list[dict],
) -> tuple[list[str], list[str], set[int]]:
    findings_fixed: list[str] = []
    findings_opened: list[str] = []
    resolved: set[int] = set()
    for index, line in enumerate(transcript_lines):
        line_lower = line.lower()
        for finding in finding_rows:
            finding_id = str(finding.get("finding_id", ""))
            if not finding_id or finding_id.lower() not in line_lower:
                continue
            if any(word in line_lower for word in ("fixed", "fix", "resolved")):
                findings_fixed.append(finding_id)
                resolved.add(index)
            if any(word in line_lower for word in ("opened", "open", "reopened")):
                findings_opened.append(finding_id)
                resolved.add(index)
    return findings_fixed, findings_opened, resolved


def _collect_test_matches(
    transcript_lines: list[str],
    test_rows: list[dict],
) -> tuple[list[str], set[int]]:
    tests_verified: list[str] = []
    resolved: set[int] = set()
    for index, line in enumerate(transcript_lines):
        for test_row in test_rows:
            command = str(test_row.get("command", ""))
            if command and command in line:
                tests_verified.append(command)
                resolved.add(index)
    return tests_verified, resolved


def _collect_touched_file_matches(
    transcript_lines: list[str],
    touch_rows: list[dict],
) -> tuple[list[str], set[int]]:
    files_touched: list[str] = []
    resolved: set[int] = set()
    for index, line in enumerate(transcript_lines):
        for touch_row in touch_rows:
            file_path = str(touch_row.get("file_path", ""))
            if file_path and file_path in line:
                files_touched.append(file_path)
                resolved.add(index)
    return files_touched, resolved


def _collect_decision_matches(
    transcript_lines: list[str],
    decisions: list[dict[str, object]],
) -> tuple[list[dict[str, str]], list[str], set[int]]:
    resolved_decisions: list[dict[str, str]] = []
    extra_files_touched: list[str] = []
    resolved: set[int] = set()
    for index, line in enumerate(transcript_lines):
        for decision in decisions:
            decision_id = str(decision["decision_id"])
            if decision_id and decision_id in line:
                resolved_decisions.append({"decision_id": decision_id, "slug": str(decision["slug"])})
                resolved.add(index)
        for decision in decisions:
            changed_files = decision["changed_files"]
            if not isinstance(changed_files, list):
                continue
            for changed_file in changed_files:
                if changed_file in line:
                    extra_files_touched.append(changed_file)
                    resolved.add(index)
    return resolved_decisions, extra_files_touched, resolved


def _derive_prose_residual(
    raw_nonempty_lines: list[str],
    transcript: str,
    resolved_line_indexes: set[int],
) -> str | None:
    if not resolved_line_indexes:
        return transcript if transcript else None
    residual_lines = [line for index, line in enumerate(raw_nonempty_lines) if index not in resolved_line_indexes]
    return "".join(residual_lines).rstrip("\n") if residual_lines else None


def _extract_summary_fields(conn: sqlite3.Connection, *, task_ref: str, transcript: str) -> dict[str, object]:
    raw_nonempty_lines = [line for line in transcript.splitlines(keepends=True) if line.strip()]
    transcript_lines = [line.strip() for line in raw_nonempty_lines]

    finding_rows = (
        list_review_findings(task_ref=task_ref, status="all", limit=500, detail="full")
        .get("data", {})
        .get("findings", [])
    )
    findings_fixed, findings_opened, finding_indexes = _collect_finding_matches(transcript_lines, finding_rows)

    test_rows = get_verified_tests(task_ref=task_ref, limit=500).get("data", {}).get("tests", [])
    tests_verified, test_indexes = _collect_test_matches(transcript_lines, test_rows)

    touch_rows = get_touched_files(task_ref=task_ref, limit=500).get("data", {}).get("touches", [])
    files_touched, touch_indexes = _collect_touched_file_matches(transcript_lines, touch_rows)

    decisions = _CompactionQueries(conn, task_ref).load_task_decisions()
    resolved_decisions, decision_files_touched, decision_indexes = _collect_decision_matches(
        transcript_lines, decisions
    )
    files_touched.extend(decision_files_touched)

    resolved_line_indexes = finding_indexes | test_indexes | touch_indexes | decision_indexes
    prose_residual = _derive_prose_residual(raw_nonempty_lines, transcript, resolved_line_indexes)

    return {
        "turn_range": _derive_turn_range(transcript),
        "decisions": _ordered_unique([json.dumps(item, sort_keys=True) for item in resolved_decisions]),
        "findings_fixed": _ordered_unique(findings_fixed),
        "findings_opened": _ordered_unique(findings_opened),
        "tests_verified": _ordered_unique(tests_verified),
        "files_touched": _ordered_unique(files_touched),
        "prose_residual": _truncate_residual(prose_residual),
    }


def _build_structured_summary(
    *,
    compaction_id: str,
    session_id: str,
    normalized_harness: CompactionHarness,
    resolved_task_ref: str,
    extracted: dict[str, object],
    created_at: datetime,
) -> StructuredSummary:
    return StructuredSummary(
        compaction_id=compaction_id,
        session_id=session_id,
        harness=normalized_harness,
        task_ref=resolved_task_ref,
        turn_range=cast(TurnRange, extracted["turn_range"]),
        decisions=[json.loads(item) for item in cast(list[str], extracted["decisions"])],
        findings_fixed=cast(list[str], extracted["findings_fixed"]),
        findings_opened=cast(list[str], extracted["findings_opened"]),
        tests_verified=cast(list[str], extracted["tests_verified"]),
        files_touched=cast(list[str], extracted["files_touched"]),
        prose_residual=cast("str | None", extracted["prose_residual"]),
        created_at=created_at,
    )


def _persist_session_compaction(
    conn: sqlite3.Connection,
    summary: StructuredSummary,
    *,
    created_at: datetime,
) -> int | None:
    cursor = conn.execute(
        """
        INSERT INTO session_compactions (
            compaction_id, session_id, harness, task_ref, turn_range,
            structured_summary_json, prose_residual, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            summary.compaction_id,
            summary.session_id,
            summary.harness,
            summary.task_ref,
            summary.turn_range.model_dump_json(),
            summary.model_dump_json(),
            summary.prose_residual,
            created_at.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    return cursor.lastrowid


def _build_record_receipt(
    summary: StructuredSummary,
    *,
    input_chars: int,
    db_row_id: int,
) -> CompactionRecordReceipt:
    summary_chars = len(summary.model_dump_json())
    prose_residual_chars = len(summary.prose_residual or "")
    tokens_saved_estimate = max(0, (input_chars - summary_chars - prose_residual_chars) // 4)
    return CompactionRecordReceipt(
        compaction_id=summary.compaction_id,
        summary=summary,
        input_chars=input_chars,
        summary_chars=summary_chars,
        prose_residual_chars=prose_residual_chars,
        tokens_saved_estimate=tokens_saved_estimate,
        db_row_id=db_row_id,
    )


def compact_session(
    transcript_path: str | Path,
    task_ref: str,
    harness: CompactionHarnessInput,
    session_id: str,
) -> CompactionRecordReceipt:
    """Persist a ``session_compactions`` row and return the typed receipt.

    WORKSTATE-REF-67 implementation note widened the return type from a bare ``compaction_id``
    string to ``CompactionRecordReceipt`` so callers can attribute the
    compression delta without a second round-trip. The receipt inlines the
    canonical ``StructuredSummary`` (WORKSTATE-REF-004); the chars/4 divisor used by
    ``tokens_saved_estimate`` is sourced from ``harness-protocol.yaml``
    lines 126-127 (WORKSTATE-REF-007). The legacy bare-string wrapper at the
    ``workstate_handoff_mcp.api`` layer was deleted alongside this widening
    after the WORKSTATE-REF-005 caller audit (decision id 662) confirmed no external
    callers depended on it.
    """
    normalized_harness = _validate_harness(harness)
    transcript = _read_transcript(transcript_path)
    now = datetime.now(UTC)

    with _get_db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        resolved_task_ref = _resolve_task_ref(conn, task_ref)
        compaction_id = _CompactionQueries(conn, resolved_task_ref).next_compaction_id()
        extracted = _extract_summary_fields(conn, task_ref=resolved_task_ref, transcript=transcript)
        summary = _build_structured_summary(
            compaction_id=compaction_id,
            session_id=session_id,
            normalized_harness=normalized_harness,
            resolved_task_ref=resolved_task_ref,
            extracted=extracted,
            created_at=now,
        )
        db_row_id = _persist_session_compaction(conn, summary, created_at=now)
        conn.commit()

    if db_row_id is None:
        raise RuntimeError("session_compactions INSERT returned no lastrowid; cannot build receipt")

    return _build_record_receipt(summary, input_chars=len(transcript), db_row_id=db_row_id)


def get_compaction(compaction_id: str) -> StructuredSummary:
    normalized_compaction_id = compaction_id.strip()
    if not normalized_compaction_id:
        raise ValueError("compaction_id is required")

    with _get_db_connection() as conn:
        row = conn.execute(
            "SELECT structured_summary_json FROM session_compactions WHERE compaction_id = ?",
            (normalized_compaction_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown compaction_id: {normalized_compaction_id}")
        return StructuredSummary.model_validate_json(str(row["structured_summary_json"]))


def render_cold_start_compaction(task_ref: str | None = None) -> str | None:
    """Render the latest compaction as an ID-only cold-start block.

    Returns ``None`` when no compaction row exists for the resolved task,
    so cold-start callers can fall back to the pre-WORKSTATE-REF-34 baseline
    without emitting any extra bytes.
    """
    with _get_db_connection() as conn:
        resolved_task_ref = _resolve_task_ref(conn, task_ref)
        row = conn.execute(
            """
            SELECT structured_summary_json, created_at
            FROM session_compactions
            WHERE task_ref = ?
            ORDER BY created_at DESC, compaction_id DESC
            LIMIT 1
            """,
            (resolved_task_ref,),
        ).fetchone()
        if row is None:
            return None
        latest = StructuredSummary.model_validate_json(str(row["structured_summary_json"]))
        # Use the row's stored created_at (string) so the comparison is
        # against the same column the row was written with.
        stored_created_at = datetime.strptime(str(row["created_at"]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        newer_decisions = _CompactionQueries(conn, resolved_task_ref).count_decisions_after(stored_created_at)

    lines: list[str] = []
    lines.append(f"Latest compaction: {latest.compaction_id}")
    lines.append(f"Turns {latest.turn_range.start_turn}–{latest.turn_range.end_turn}")
    decision_ids = [decision.decision_id for decision in latest.decisions]
    lines.append(f"Decisions: {', '.join(decision_ids) if decision_ids else '(none)'}")
    lines.append(f"Findings fixed: {', '.join(latest.findings_fixed) if latest.findings_fixed else '(none)'}")
    lines.append(f"Tests verified: {', '.join(latest.tests_verified) if latest.tests_verified else '(none)'}")
    lines.append(f"Files touched: {', '.join(latest.files_touched) if latest.files_touched else '(none)'}")
    if newer_decisions > 0:
        lines.append(f"(compaction stale; {newer_decisions} decisions newer)")
    return "\n".join(lines)


def get_latest_compaction(task_ref: str | None = None) -> StructuredSummary | None:
    with _get_db_connection() as conn:
        resolved_task_ref = _resolve_task_ref(conn, task_ref)
        row = conn.execute(
            """
            SELECT structured_summary_json
            FROM session_compactions
            WHERE task_ref = ?
            ORDER BY created_at DESC, compaction_id DESC
            LIMIT 1
            """,
            (resolved_task_ref,),
        ).fetchone()
        if row is None:
            return None
        return StructuredSummary.model_validate_json(str(row["structured_summary_json"]))


def _load_latest_compaction_summary(
    conn: sqlite3.Connection,
    resolved_task_ref: str,
) -> tuple[StructuredSummary, int] | None:
    row = conn.execute(
        """
        SELECT structured_summary_json, created_at
        FROM session_compactions
        WHERE task_ref = ?
        ORDER BY created_at DESC, compaction_id DESC
        LIMIT 1
        """,
        (resolved_task_ref,),
    ).fetchone()
    if row is None:
        return None
    latest = StructuredSummary.model_validate_json(str(row["structured_summary_json"]))
    stored_created_at = datetime.strptime(str(row["created_at"]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    newer_decisions = _CompactionQueries(conn, resolved_task_ref).count_decisions_after(stored_created_at)
    return latest, newer_decisions


def _build_refresh_packet_body(
    latest: StructuredSummary,
    *,
    resolved_task_ref: str,
    dedupe_key: str,
    rendered: str | None,
    newer_decisions: int,
    advisory: dict[str, object] | None,
) -> dict[str, object]:
    latest_json = latest.model_dump(mode="json")
    return {
        "task_ref": resolved_task_ref,
        "compaction_id": latest.compaction_id,
        "created_at": latest_json.get("created_at"),
        "session_id": latest.session_id,
        "harness": latest.harness,
        "policy": "supersedes_prior_session_detail",
        "dedupe_key": dedupe_key,
        "rendered_cold_start": rendered,
        "stale": {
            "detected": newer_decisions > 0,
            "decisions_newer": newer_decisions,
        },
        "advisory": advisory,
    }


def build_context_refresh_packet(
    task_ref: str | None = None,
    *,
    last_injected_compaction_id: str | None = None,
    advisory: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build an opt-in same-session context refresh packet from the latest compaction."""
    with _get_db_connection() as conn:
        resolved_task_ref = _resolve_task_ref(conn, task_ref)
        loaded = _load_latest_compaction_summary(conn, resolved_task_ref)
    if loaded is None:
        return {
            "available": False,
            "reason": "no_compaction",
            "dedupe_key": None,
            "packet": None,
        }
    latest, newer_decisions = loaded

    dedupe_key = latest.compaction_id
    if last_injected_compaction_id is not None and last_injected_compaction_id.strip() == latest.compaction_id:
        return {
            "available": False,
            "reason": "already_injected",
            "dedupe_key": dedupe_key,
            "packet": None,
        }

    rendered = render_cold_start_compaction(task_ref=resolved_task_ref)
    return {
        "available": True,
        "reason": "new_packet",
        "dedupe_key": dedupe_key,
        "packet": _build_refresh_packet_body(
            latest,
            resolved_task_ref=resolved_task_ref,
            dedupe_key=dedupe_key,
            rendered=rendered,
            newer_decisions=newer_decisions,
            advisory=advisory,
        ),
    }


def _skip_advisory(warnings: list[str]) -> dict:
    return CompactionAdvisory(warnings=warnings).model_dump(mode="json")


def _disabled_advisory(
    source: DisabledSource,
    *,
    warnings: list[str],
    harness: str | None,
    transcript_path: str | None,
    transcript_source: str | None,
    contract_source: dict | None = None,
) -> dict:
    """Build the advisory envelope returned when the resolver short-circuits.

    Threshold/observed math is skipped entirely (the resolver short-circuits
    *before* any comparison). The harness/transcript fields remain present
    for traceability so callers can still see what the advisory would have
    measured if it had run.
    """
    return CompactionAdvisory(
        harness=harness,
        transcript=CompactionAdvisoryTranscript(path=transcript_path, source=transcript_source),
        contract_source=contract_source,
        disabled=True,
        disabled_source=source,
        warnings=warnings,
    ).model_dump(mode="json")


def _glob_matches_for_harness(rule: TranscriptDiscoveryRule) -> list[Path]:
    expanded = Path(rule.fallback_glob).expanduser()
    if expanded.is_absolute():
        candidates = list(Path(expanded.anchor or "/").glob(str(expanded.relative_to(expanded.anchor))))
    else:
        candidates = list(Path.cwd().glob(str(expanded)))
    return sorted(candidates, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)


def _detect_harness_from_fallback_glob(
    contract: CompactionContract,
) -> CompactionContractHarness | None:
    """Resolve harness via fallback_glob when env vars are unset.

    WORKSTATE-REF-61-BR-02 follow-up: WORKSTATE-REF-1 documents transcript discovery as env-var
    first, fallback_glob second. When exactly one harness produces matches on
    disk, use it; ambiguous or empty results remain unresolved.
    """

    matches: list[CompactionContractHarness] = []
    for harness_name, rule in contract.transcript_discovery.items():
        if _glob_matches_for_harness(rule):
            matches.append(normalize_compaction_harness(harness_name))
    if len(matches) == 1:
        return matches[0]
    return None


def _resolve_transcript(
    contract: CompactionContract,
    harness: str | None,
    env: Mapping[str, str],
) -> tuple[str | None, str | None, str | None]:
    """Return (path, source, warning) for the resolved transcript."""

    if harness is None or harness not in contract.transcript_discovery:
        return None, None, None
    rule = contract.transcript_discovery[harness]
    env_value = env.get(rule.env_var, "").strip()
    if env_value:
        candidate = Path(env_value).expanduser()
        if candidate.exists():
            return str(candidate), "env_var", None
        return (
            None,
            None,
            f"transcript path from {rule.env_var} does not exist: {candidate}",
        )

    matches = _glob_matches_for_harness(rule)
    if matches:
        return str(matches[0]), "fallback_glob", None
    return (
        None,
        None,
        f"no transcript found for harness {harness!r} (env {rule.env_var} unset, fallback_glob had no matches)",
    )


def _build_drift_warning(contract_source: Mapping[str, object]) -> str | None:
    drift = contract_source.get("drift")
    if not (isinstance(drift, dict) and drift.get("detected")):
        return None
    drift_thresholds = drift.get("thresholds") or {}
    parts: list[str] = []
    for key in ("tokens", "chars"):
        entry = drift_thresholds.get(key)
        if isinstance(entry, dict):
            parts.append(f"{key} resolved={entry.get('resolved')} package_reference={entry.get('package_reference')}")
    summary = "; ".join(parts) if parts else "thresholds differ"
    return f"compaction_contract_drift: resolved contract differs from package reference ({summary})"


def _resolve_harness_with_fallback(
    contract: CompactionContract,
    env: Mapping[str, str],
) -> tuple[CompactionContractHarness | None, list[str]]:
    resolution = detect_active_harness(contract, env=dict(env))
    harness = resolution.harness
    if harness is None:
        harness = _detect_harness_from_fallback_glob(contract)
    extra_warnings: list[str] = [] if harness is not None else list(resolution.warnings)
    return harness, extra_warnings


def _measure_transcript_chars(transcript_path: str | None) -> tuple[int | None, str | None]:
    if transcript_path is None:
        return None, None
    try:
        return len(Path(transcript_path).read_text(encoding="utf-8", errors="replace")), None
    except OSError as exc:
        return None, f"unreadable transcript at {transcript_path}: {exc}"


def _latest_compaction_cursor(task_ref: str) -> tuple[str | None, datetime | None]:
    """Return (latest_id, since) for the latest compaction of task_ref."""

    latest = get_latest_compaction(task_ref=task_ref)
    if latest is None:
        return None, None
    ts_raw = getattr(latest, "created_at", None)
    since = ts_raw if isinstance(ts_raw, datetime) else None
    return latest.compaction_id, since


def compute_compaction_advisory(
    *,
    workspace_root: str | Path,
    task_ref: str,
    env: Mapping[str, str] | None = None,
) -> dict:
    """WORKSTATE-REF-61 implementation note — contract-driven compaction advisory evaluator.

    Returns the canonical advisory envelope documented in the WORKSTATE-REF-61
    task plan (`recommended`, `thresholds`, `observed`, `harness`,
    `transcript`, `latest_compaction_id`, `warnings`).
    """

    source_env: Mapping[str, str] = env if env is not None else os.environ
    try:
        contract = load_compaction_contract(workspace_root)
    except FileNotFoundError as exc:
        return _skip_advisory([f"missing compaction contract: {exc}"])
    except (ValueError, OSError) as exc:
        return _skip_advisory([f"unreadable compaction contract: {exc}"])

    warnings: list[str] = []
    contract_source = build_contract_source_report(contract, workspace_root=workspace_root).model_dump(mode="json")
    drift_warning = _build_drift_warning(contract_source)
    if drift_warning is not None:
        warnings.append(drift_warning)
    harness, harness_warnings = _resolve_harness_with_fallback(contract, source_env)
    warnings.extend(harness_warnings)

    transcript_path, transcript_source, transcript_warning = _resolve_transcript(contract, harness, source_env)
    if transcript_warning:
        warnings.append(transcript_warning)

    observed_chars, transcript_read_warning = _measure_transcript_chars(transcript_path)
    if transcript_read_warning is not None:
        warnings.append(transcript_read_warning)

    latest_id, since = _latest_compaction_cursor(task_ref)

    with _get_db_connection() as conn:
        disabled, disabled_source = resolve_compaction_disabled(env=source_env, conn=conn, task_ref=task_ref)
        if disabled and disabled_source is not None:
            return _disabled_advisory(
                disabled_source,
                warnings=warnings,
                harness=harness,
                transcript_path=transcript_path,
                transcript_source=transcript_source,
                contract_source=contract_source,
            )
        observed_tokens = _CompactionQueries(conn, task_ref).observed_token_total(since)

    effective = resolve_effective_thresholds(
        contract,
        env=source_env,
        workspace_root=Path(workspace_root) if not isinstance(workspace_root, Path) else workspace_root,
    )
    warnings.extend(effective.warnings)

    recommended = False
    if observed_tokens is not None and observed_tokens >= effective.tokens:
        recommended = True
    char_gate_eligible = latest_id is None or (observed_tokens is not None and observed_tokens > 0)
    if char_gate_eligible and observed_chars is not None and observed_chars >= effective.chars:
        recommended = True

    return CompactionAdvisory(
        recommended=recommended,
        recommended_action="compaction(operation=record)" if recommended else None,
        thresholds=CompactionAdvisoryThresholds(tokens=effective.tokens, chars=effective.chars),
        thresholds_source=CompactionAdvisoryThresholdSources(
            tokens=effective.tokens_source,
            chars=effective.chars_source,
        ),
        observed=CompactionAdvisoryThresholds(tokens=observed_tokens, chars=observed_chars),
        harness=harness,
        transcript=CompactionAdvisoryTranscript(path=transcript_path, source=transcript_source),
        contract_source=contract_source,
        latest_compaction_id=latest_id,
        warnings=warnings,
    ).model_dump(mode="json")
