from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import jsonschema
import pytest
from workstate_protocol import StructuredSummary, TurnRange

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig
from workstate_handoff_mcp.shared_schema import _get_db_connection


@pytest.fixture()
def isolated_runtime(tmp_path: Path) -> RuntimeConfig:
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    mcp_server.configure_runtime(runtime)
    return runtime


def _seed_compaction(
    *,
    task_ref: str,
    compaction_id: str,
    created_at: str,
    session_id: str,
) -> StructuredSummary:
    summary = StructuredSummary(
        compaction_id=compaction_id,
        session_id=session_id,
        harness="codex",
        task_ref=task_ref,
        turn_range=TurnRange(start_turn=1, end_turn=42),
        decisions=[{"decision_id": "scope_intake_WORKSTATE-34_trigger_choice", "slug": "trigger-choice"}],
        findings_fixed=["F-1"],
        findings_opened=["F-2"],
        tests_verified=["pytest tests/test_schema_migrations.py -q"],
        files_touched=["packages/mcp-workstate-handoff/src/workstate_handoff_mcp/shared_schema.py"],
        prose_residual="Residual context",
        created_at=created_at.replace(" ", "T") + "Z",
    )
    with _get_db_connection() as conn:
        conn.execute(
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
                created_at,
            ),
        )
        conn.commit()
    return summary


def _seed_task_records(task_ref: str) -> None:
    with _get_db_connection() as conn:
        conn.execute(
            "INSERT INTO decisions (task_ref, session, decision, rationale, changed_files_json) VALUES (?, ?, ?, ?, ?)",
            (
                task_ref,
                "seed-session",
                "scope_intake_WORKSTATE-34_trigger_choice",
                "seed rationale",
                json.dumps(["packages/mcp-workstate-handoff/src/workstate_handoff_mcp/shared_schema.py"]),
            ),
        )
        conn.execute(
            """
            INSERT INTO review_findings (
                task_ref, finding_id, severity, file_path, description, status, session
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (task_ref, "F-1", "medium", "src/demo.py", "fixed finding", "fixed", "seed-session"),
        )
        conn.execute(
            """
            INSERT INTO review_findings (
                task_ref, finding_id, severity, file_path, description, status, session
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (task_ref, "F-2", "medium", "src/demo.py", "opened finding", "open", "seed-session"),
        )
        conn.execute(
            """
            INSERT INTO verified_tests (
                task_ref, command, passed, result, session
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (task_ref, "pytest tests/test_schema_migrations.py -q", 1, "7 passed", "seed-session"),
        )
        conn.execute(
            """
            INSERT INTO touched_files (
                task_ref, file_path, change_kind, session
            ) VALUES (?, ?, ?, ?)
            """,
            (
                task_ref,
                "packages/mcp-workstate-handoff/src/workstate_handoff_mcp/shared_schema.py",
                "edit",
                "seed-session",
            ),
        )
        conn.commit()


def _compaction_schema() -> dict:
    schema_path = Path(__file__).resolve().parents[2] / "workstate-protocol" / "schemas" / "compaction-summary.json"
    return json.loads(schema_path.read_text())


def test_get_compaction_dereferences_by_id(isolated_runtime: RuntimeConfig) -> None:
    expected = _seed_compaction(
        task_ref="WORKSTATE-REF-34",
        compaction_id="C-WORKSTATE-REF-34-0001",
        created_at="2026-04-30 23:00:00",
        session_id="session-123",
    )

    actual = mcp_server.get_compaction("C-WORKSTATE-REF-34-0001")

    assert isinstance(actual, StructuredSummary)
    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")


def test_get_latest_compaction_returns_newest(isolated_runtime: RuntimeConfig) -> None:
    _seed_compaction(
        task_ref="WORKSTATE-REF-34",
        compaction_id="C-WORKSTATE-REF-34-0001",
        created_at="2026-04-30 23:00:00",
        session_id="session-old",
    )
    expected = _seed_compaction(
        task_ref="WORKSTATE-REF-34",
        compaction_id="C-WORKSTATE-REF-34-0002",
        created_at="2026-04-30 23:05:00",
        session_id="session-new",
    )
    _seed_compaction(
        task_ref="OTHER-1",
        compaction_id="C-OTHER-1-0001",
        created_at="2026-04-30 23:10:00",
        session_id="other-session",
    )

    actual = mcp_server.get_latest_compaction(task_ref="WORKSTATE-REF-34")

    assert isinstance(actual, StructuredSummary)
    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")


def test_get_latest_compaction_returns_none_when_absent(isolated_runtime: RuntimeConfig) -> None:
    assert mcp_server.get_latest_compaction(task_ref="WORKSTATE-REF-34") is None


def test_compact_session_persists_row_and_returns_id(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    transcript_path = tmp_path / "transcript.md"
    transcript_path.write_text("Residual-only transcript content for compaction.\n")

    receipt = mcp_server.compact_session(
        transcript_path=transcript_path,
        task_ref="WORKSTATE-REF-34",
        harness="codex",
        session_id="session-compact-1",
    )
    compaction_id = receipt.compaction_id

    assert compaction_id == "C-WORKSTATE-REF-34-0001"
    stored = mcp_server.get_compaction(compaction_id)
    assert stored.compaction_id == compaction_id
    assert stored.session_id == "session-compact-1"
    assert stored.harness == "codex"
    assert stored.task_ref == "WORKSTATE-REF-34"
    assert stored.turn_range.model_dump() == {"start_turn": 1, "end_turn": 1}
    assert stored.prose_residual == "Residual-only transcript content for compaction.\n"


def test_compact_session_increments_suffix_per_task(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    first_path = tmp_path / "first.md"
    second_path = tmp_path / "second.md"
    first_path.write_text("first transcript\n")
    second_path.write_text("second transcript\n")

    first_id = mcp_server.compact_session(
        transcript_path=first_path,
        task_ref="WORKSTATE-REF-34",
        harness="codex",
        session_id="session-compact-1",
    ).compaction_id
    second_id = mcp_server.compact_session(
        transcript_path=second_path,
        task_ref="WORKSTATE-REF-34",
        harness="codex",
        session_id="session-compact-2",
    ).compaction_id

    assert first_id == "C-WORKSTATE-REF-34-0001"
    assert second_id == "C-WORKSTATE-REF-34-0002"
    latest = mcp_server.get_latest_compaction(task_ref="WORKSTATE-REF-34")
    assert latest is not None
    assert latest.compaction_id == second_id
    assert latest.session_id == "session-compact-2"


def test_compact_session_rejects_unknown_harness(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    transcript_path = tmp_path / "transcript.md"
    transcript_path.write_text("turn 1\n")

    with pytest.raises(
        ValueError,
        match=r"record\.harness|Input should be 'claude-code'|Invalid harness:",
    ):
        mcp_server.compact_session(
            transcript_path=transcript_path,
            task_ref="WORKSTATE-REF-34",
            harness="unknown-harness",  # type: ignore[arg-type]
            session_id="session-compact-1",
        )


def test_compaction_id_uniqueness_per_task(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    sequential_ids: list[str] = []
    for index in range(100):
        transcript_path = tmp_path / f"seq-{index}.md"
        transcript_path.write_text(f"Turn {index + 1}: sequential transcript\n")
        sequential_ids.append(
            mcp_server.compact_session(
                transcript_path=transcript_path,
                task_ref="WORKSTATE-REF-34",
                harness="codex",
                session_id=f"session-seq-{index}",
            ).compaction_id
        )
    assert sequential_ids[0] == "C-WORKSTATE-REF-34-0001"
    assert sequential_ids[-1] == "C-WORKSTATE-REF-34-0100"
    assert len(set(sequential_ids)) == 100

    def _parallel_compact(index: int) -> str:
        mcp_server.configure_runtime(isolated_runtime)
        transcript_path = tmp_path / f"par-{index}.md"
        transcript_path.write_text(f"Turn {index + 101}: parallel transcript\n")
        return mcp_server.compact_session(
            transcript_path=transcript_path,
            task_ref="WORKSTATE-REF-34",
            harness="codex",
            session_id=f"session-par-{index}",
        ).compaction_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        parallel_ids = list(executor.map(_parallel_compact, range(8)))
    assert len(set(parallel_ids)) == 8
    assert sorted(parallel_ids)[0] == "C-WORKSTATE-REF-34-0101"
    assert sorted(parallel_ids)[-1] == "C-WORKSTATE-REF-34-0108"


def test_compaction_id_suffix_overflow_raises(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    _seed_compaction(
        task_ref="WORKSTATE-REF-34",
        compaction_id="C-WORKSTATE-REF-34-9999",
        created_at="2026-04-30 23:59:59",
        session_id="session-overflow",
    )
    transcript_path = tmp_path / "overflow.md"
    transcript_path.write_text("Turn 1: overflow\n")

    with pytest.raises(ValueError, match="overflow"):
        mcp_server.compact_session(
            transcript_path=transcript_path,
            task_ref="WORKSTATE-REF-34",
            harness="codex",
            session_id="session-after-overflow",
        )


def test_extractor_resolves_known_ids(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    _seed_task_records("WORKSTATE-REF-34")
    transcript_path = tmp_path / "resolved.md"
    transcript_path.write_text(
        "\n".join(
            [
                "Turn 3: fixed F-1 after scope_intake_WORKSTATE-34_trigger_choice.",
                "Turn 4: verified pytest tests/test_schema_migrations.py -q.",
                "Turn 5: opened F-2 while editing packages/mcp-workstate-handoff/src/workstate_handoff_mcp/shared_schema.py.",
            ]
        )
        + "\n"
    )

    compaction_id = mcp_server.compact_session(
        transcript_path=transcript_path,
        task_ref="WORKSTATE-REF-34",
        harness="codex",
        session_id="session-extract-1",
    ).compaction_id
    stored = mcp_server.get_compaction(compaction_id)

    assert stored.turn_range.model_dump() == {"start_turn": 3, "end_turn": 5}
    assert [decision.decision_id for decision in stored.decisions] == ["scope_intake_WORKSTATE-34_trigger_choice"]
    assert stored.findings_fixed == ["F-1"]
    assert stored.findings_opened == ["F-2"]
    assert stored.tests_verified == ["pytest tests/test_schema_migrations.py -q"]
    assert stored.files_touched == ["packages/mcp-workstate-handoff/src/workstate_handoff_mcp/shared_schema.py"]


def test_extractor_preserves_unresolved_spans_as_residual(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    _seed_task_records("WORKSTATE-REF-34")
    transcript_path = tmp_path / "residual.md"
    transcript_path.write_text(
        "Turn 7: fixed F-1 after scope_intake_WORKSTATE-34_trigger_choice.\n"
        "This narrative sentence has no structured ids and must stay residual.\n"
    )

    compaction_id = mcp_server.compact_session(
        transcript_path=transcript_path,
        task_ref="WORKSTATE-REF-34",
        harness="codex",
        session_id="session-extract-2",
    ).compaction_id
    stored = mcp_server.get_compaction(compaction_id)

    assert stored.findings_fixed == ["F-1"]
    assert stored.prose_residual == "This narrative sentence has no structured ids and must stay residual."


def test_extractor_truncates_oversize_residual(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    soft_transcript = tmp_path / "soft.md"
    soft_transcript.write_text("x" * 5000)

    compaction_id = mcp_server.compact_session(
        transcript_path=soft_transcript,
        task_ref="WORKSTATE-REF-34",
        harness="codex",
        session_id="session-soft",
    ).compaction_id
    stored = mcp_server.get_compaction(compaction_id)
    assert stored.prose_residual is not None
    assert stored.prose_residual.endswith("chars omitted]")
    assert len(stored.prose_residual) < 5000

    hard_transcript = tmp_path / "hard.md"
    hard_transcript.write_text("y" * 17000)
    with pytest.raises(ValueError, match="hard limit"):
        mcp_server.compact_session(
            transcript_path=hard_transcript,
            task_ref="WORKSTATE-REF-34",
            harness="codex",
            session_id="session-hard",
        )


def test_cold_start_no_compaction_matches_baseline(isolated_runtime: RuntimeConfig) -> None:
    """No compaction row -> render returns None so the caller emits the
    pre-WORKSTATE-REF-34 byte-identical baseline (i.e., nothing extra)."""
    assert mcp_server.render_cold_start_compaction(task_ref="WORKSTATE-REF-34") is None


def test_cold_start_renders_structured_summary(isolated_runtime: RuntimeConfig) -> None:
    """A latest compaction renders an ID-only block (no prose) with the
    five canonical sections from the structured summary."""
    _seed_compaction(
        task_ref="WORKSTATE-REF-34",
        compaction_id="C-WORKSTATE-REF-34-0001",
        created_at="2026-04-30 23:00:00",
        session_id="session-cold-1",
    )

    rendered = mcp_server.render_cold_start_compaction(task_ref="WORKSTATE-REF-34")

    assert rendered is not None
    assert "C-WORKSTATE-REF-34-0001" in rendered
    assert "Turns 1" in rendered and "42" in rendered
    assert "scope_intake_WORKSTATE-34_trigger_choice" in rendered
    assert "F-1" in rendered
    assert "pytest tests/test_schema_migrations.py -q" in rendered
    assert "packages/mcp-workstate-handoff/src/workstate_handoff_mcp/shared_schema.py" in rendered
    # Cold-start surface is ID-only; prose_residual must not bleed in.
    assert "Residual context" not in rendered


def test_cold_start_dereferences_by_compaction_id(isolated_runtime: RuntimeConfig) -> None:
    """The newer compaction wins; rendering routes through the
    compaction_id path, not a rowid scan."""
    _seed_compaction(
        task_ref="WORKSTATE-REF-34",
        compaction_id="C-WORKSTATE-REF-34-0001",
        created_at="2026-04-30 23:00:00",
        session_id="session-cold-old",
    )
    _seed_compaction(
        task_ref="WORKSTATE-REF-34",
        compaction_id="C-WORKSTATE-REF-34-0002",
        created_at="2026-04-30 23:05:00",
        session_id="session-cold-new",
    )

    rendered = mcp_server.render_cold_start_compaction(task_ref="WORKSTATE-REF-34")

    assert rendered is not None
    assert "C-WORKSTATE-REF-34-0002" in rendered
    assert "C-WORKSTATE-REF-34-0001" not in rendered


def test_cold_start_stale_compaction_emits_advisory(isolated_runtime: RuntimeConfig) -> None:
    """When newer decisions exist after the latest compaction's
    created_at, the renderer appends a 'compaction stale' advisory."""
    _seed_compaction(
        task_ref="WORKSTATE-REF-34",
        compaction_id="C-WORKSTATE-REF-34-0001",
        created_at="2026-04-30 12:00:00",
        session_id="session-stale",
    )
    with _get_db_connection() as conn:
        conn.execute(
            "INSERT INTO decisions (task_ref, session, decision, rationale, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                "WORKSTATE-REF-34",
                "post-compaction",
                "post_compaction_decision_1",
                "newer than the compaction",
                "2026-05-01 09:00:00",
            ),
        )
        conn.execute(
            "INSERT INTO decisions (task_ref, session, decision, rationale, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                "WORKSTATE-REF-34",
                "post-compaction",
                "post_compaction_decision_2",
                "also newer",
                "2026-05-01 09:05:00",
            ),
        )
        conn.commit()

    rendered = mcp_server.render_cold_start_compaction(task_ref="WORKSTATE-REF-34")

    assert rendered is not None
    assert "stale" in rendered.lower()
    assert "2" in rendered  # number of newer decisions


def test_current_task_render_includes_cold_start_compaction(isolated_runtime: RuntimeConfig) -> None:
    """Cold-start compaction is queryable via render_cold_start_compaction; the
    WORKSTATE-REF-54 v2 workspace summary deliberately omits it from CURRENT_TASK.json
    (the slim summary shape carries no per-task narrative blocks)."""
    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-34",
        objective="BR-03 wiring probe",
        status="in_progress",
    )
    _seed_compaction(
        task_ref="WORKSTATE-REF-34",
        compaction_id="C-WORKSTATE-REF-34-0001",
        created_at="2026-04-30 23:00:00",
        session_id="session-cold-wired",
    )

    block = mcp_server.render_cold_start_compaction(task_ref="WORKSTATE-REF-34")
    assert isinstance(block, str) and "C-WORKSTATE-REF-34-0001" in block


def test_current_task_render_omits_cold_start_when_no_compaction(isolated_runtime: RuntimeConfig) -> None:
    """No compaction row -> the key must be absent so the byte-identical
    pre-WORKSTATE-REF-34 baseline is preserved."""
    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-34",
        objective="BR-03 baseline probe",
        status="in_progress",
    )

    rendered = json.loads(
        json.loads(json.dumps(mcp_server.render_handoff(kind="current_task", task_ref="WORKSTATE-REF-34", write_file=False)))[
            "data"
        ]["current_task_json"]
    )

    assert "cold_start_compaction" not in rendered, (
        f"baseline must omit cold_start_compaction; payload keys={sorted(rendered)}"
    )


def test_structured_summary_json_roundtrip(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    _seed_task_records("WORKSTATE-REF-34")
    transcript_path = tmp_path / "roundtrip.md"
    transcript_path.write_text(
        "Turn 9: fixed F-1 after scope_intake_WORKSTATE-34_trigger_choice.\n"
        "Turn 10: verified pytest tests/test_schema_migrations.py -q.\n"
        "Residual note without ids.\n"
    )

    compaction_id = mcp_server.compact_session(
        transcript_path=transcript_path,
        task_ref="WORKSTATE-REF-34",
        harness="codex",
        session_id="session-roundtrip",
    ).compaction_id
    with _get_db_connection() as conn:
        row = conn.execute(
            "SELECT structured_summary_json FROM session_compactions WHERE compaction_id = ?",
            (compaction_id,),
        ).fetchone()
    assert row is not None
    raw_json = str(row["structured_summary_json"])
    parsed = StructuredSummary.model_validate_json(raw_json)
    decoded = json.loads(raw_json)
    jsonschema.validate(decoded, _compaction_schema())
    assert json.dumps(json.loads(parsed.model_dump_json()), sort_keys=True) == json.dumps(decoded, sort_keys=True)


# ---------------------------------------------------------------------------
# CompactionSettings — typed env-var consolidation surface.
#
# The Pydantic model owns parsing for the canonical
# `WORKSTATE_HANDOFF_COMPACTION_*` names. Bad values raise
# `pydantic.ValidationError` so typos become loud failures rather than
# silent default fallbacks (the old `_read_int_env` behavior).
# ---------------------------------------------------------------------------


def test_compaction_settings_defaults_when_unset() -> None:
    from workstate_handoff_mcp.compaction import CompactionSettings

    settings = CompactionSettings.from_env(env={})
    assert settings.disabled is False
    assert settings.min_new_turns == 1
    assert settings.min_new_tokens == 0


def test_compaction_settings_reads_workstate_canonical_env_names() -> None:
    """implementation note B4: the top-tier ``WORKSTATE_HANDOFF_COMPACTION_*``
    names read silently."""
    from workstate_handoff_mcp.compaction import CompactionSettings

    env = {
        "WORKSTATE_HANDOFF_COMPACTION_DISABLED": "1",
        "WORKSTATE_HANDOFF_COMPACTION_MIN_NEW_TURNS": "5",
        "WORKSTATE_HANDOFF_COMPACTION_MIN_NEW_TOKENS": "250",
    }
    settings = CompactionSettings.from_env(env=env)
    assert settings.disabled is True
    assert settings.min_new_turns == 5
    assert settings.min_new_tokens == 250


def test_compaction_settings_invalid_int_raises_validation_error() -> None:
    from pydantic import ValidationError

    from workstate_handoff_mcp.compaction import CompactionSettings

    env = {"WORKSTATE_HANDOFF_COMPACTION_MIN_NEW_TOKENS": "not-an-int"}
    with pytest.raises(ValidationError) as excinfo:
        CompactionSettings.from_env(env=env)
    # Surface the offending env-var name in the error so downstream
    # callers (the Stop hook) can render `compaction failed: invalid
    # setting <name>=<value>` without re-parsing the message.
    assert "min_new_tokens" in str(excinfo.value)


def test_compaction_settings_negative_int_rejected() -> None:
    from pydantic import ValidationError

    from workstate_handoff_mcp.compaction import CompactionSettings

    env = {"WORKSTATE_HANDOFF_COMPACTION_MIN_NEW_TURNS": "-5"}
    with pytest.raises(ValidationError):
        CompactionSettings.from_env(env=env)
