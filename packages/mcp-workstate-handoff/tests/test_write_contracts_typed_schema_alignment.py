"""implementation note implementation note — BR-05 typed-schema alignment.

The write-contract registry must mirror the real Pydantic schemas in
``review_findings_api.py`` and ``api.ReviewRuns*Op``. Drift produces
silent registry-vs-API contradictions: a payload the registry green-
lights would still bounce off the typed dispatch, defeating the
"single source of truth" claim of the registry.

Pinned drift points (registry currently wrong → real schema):

* ``review_findings.record`` requires ``session, finding_id, severity,
  file_path, description``; severity is ``high|medium|low``. Registry
  used ``summary`` and ``blocker|major|minor|nit|info``.
* ``review_findings.batch_record`` requires ``session`` alongside
  ``findings``. Registry omitted ``session``.
* ``review_findings.update`` carries optional ``resolution_notes`` and
  ``reopen_reason``. Registry omitted both.
* ``review_findings.resolve`` is a real op variant. Registry omitted it.
* ``review_findings.repair_provenance`` requires
  ``session, finding_id, expected_branch, expected_commit_sha,
  new_branch, new_commit_sha, reason``. Registry only had
  ``finding_id_or_db_id``.
* ``review_findings.merge`` requires ``source_task_refs`` and
  ``target_task_ref``. Registry had legacy ``finding_ids``.
* ``review_runs.record`` requires ``review_run_id, session,
  subject_path``; ``verdict`` is optional. Registry had
  ``review_mode, subject_kind, subject_path, verdict`` and a wrong
  required set.
"""

from __future__ import annotations


def test_review_findings_record_uses_real_pydantic_shape() -> None:
    from workstate_handoff_mcp.write_contracts import get_write_contract

    contract = get_write_contract("review_findings")
    assert contract is not None
    record_variant = contract.variants["record"]
    required = set(record_variant.required)
    assert {"session", "finding_id", "severity", "file_path", "description"} <= required
    assert "summary" not in required
    severity_grammar = record_variant.field_grammars.get("severity")
    assert severity_grammar is not None
    assert "high" in severity_grammar
    assert "medium" in severity_grammar
    assert "low" in severity_grammar
    assert "blocker" not in severity_grammar
    assert "major" not in severity_grammar


def test_review_findings_batch_record_requires_session() -> None:
    from workstate_handoff_mcp.write_contracts import get_write_contract

    contract = get_write_contract("review_findings")
    assert contract is not None
    batch_variant = contract.variants["batch_record"]
    assert "session" in batch_variant.required
    assert "findings" in batch_variant.required


def test_review_findings_update_carries_resolution_and_reopen_fields() -> None:
    from workstate_handoff_mcp.write_contracts import get_write_contract

    contract = get_write_contract("review_findings")
    assert contract is not None
    update_variant = contract.variants["update"]
    assert "resolution_notes" in update_variant.optional
    assert "reopen_reason" in update_variant.optional
    assert "verified_commit_sha" in update_variant.optional
    assert "verification_evidence" in update_variant.optional


def test_review_findings_has_resolve_variant() -> None:
    from workstate_handoff_mcp.write_contracts import get_write_contract

    contract = get_write_contract("review_findings")
    assert contract is not None
    assert "resolve" in contract.variants
    resolve_variant = contract.variants["resolve"]
    assert "all_open" in resolve_variant.optional
    assert "finding_ids" in resolve_variant.optional


def test_review_findings_repair_provenance_requires_full_seven_field_shape() -> None:
    from workstate_handoff_mcp.write_contracts import get_write_contract

    contract = get_write_contract("review_findings")
    assert contract is not None
    repair_variant = contract.variants["repair_provenance"]
    required = set(repair_variant.required)
    assert {
        "session",
        "finding_id",
        "expected_branch",
        "expected_commit_sha",
        "new_branch",
        "new_commit_sha",
        "reason",
    } <= required


def test_review_findings_merge_requires_task_ref_fields_not_finding_ids() -> None:
    from workstate_handoff_mcp.write_contracts import get_write_contract

    contract = get_write_contract("review_findings")
    assert contract is not None
    merge_variant = contract.variants["merge"]
    required = set(merge_variant.required)
    assert "source_task_refs" in required
    assert "target_task_ref" in required
    assert "finding_ids" not in required


def test_review_runs_record_requires_review_run_id_and_session() -> None:
    from workstate_handoff_mcp.write_contracts import get_write_contract

    contract = get_write_contract("review_runs")
    assert contract is not None
    record_variant = contract.variants["record"]
    required = set(record_variant.required)
    assert "review_run_id" in required
    assert "session" in required
    assert "subject_path" in required
    assert "verdict" not in required


def test_validate_write_review_findings_record_accepts_real_payload() -> None:
    from workstate_handoff_mcp.write_contracts import validate_write

    result = validate_write(
        "review_findings",
        {
            "operation": "record",
            "session": "session-1",
            "finding_id": "WORKSTATE-demo-001",
            "severity": "high",
            "file_path": "packages/foo/bar.py",
            "description": "demo finding",
        },
    )
    assert result["ok"] is True, result


def test_validate_write_review_findings_record_rejects_legacy_severity() -> None:
    from workstate_handoff_mcp.write_contracts import validate_write

    result = validate_write(
        "review_findings",
        {
            "operation": "record",
            "session": "session-1",
            "finding_id": "WORKSTATE-demo-001",
            "severity": "blocker",
            "file_path": "packages/foo/bar.py",
            "description": "demo finding",
        },
    )
    assert result["ok"] is False
    assert any("severity" in err for err in result["errors"])


def test_terminal_guard_telemetry_contract_has_record_and_list_variants() -> None:
    from workstate_handoff_mcp.write_contracts import get_write_contract

    contract = get_write_contract("terminal_guard_telemetry")
    assert contract is not None
    assert "record" in contract.variants
    assert "list" in contract.variants

    record_variant = contract.variants["record"]
    assert {
        "harness",
        "tool_name",
        "decision",
        "trigger",
        "command_preview",
        "policy_version",
        "policy_source",
    } <= set(record_variant.required)

    list_variant = contract.variants["list"]
    assert "task_ref" in list_variant.optional
    assert "decision" in list_variant.optional
    assert "harness" in list_variant.optional


def test_validate_write_terminal_guard_telemetry_record_accepts_real_payload() -> None:
    from workstate_handoff_mcp.write_contracts import validate_write

    result = validate_write(
        "terminal_guard_telemetry",
        {
            "operation": "record",
            "task_ref": "WORKSTATE-REF-59",
            "harness": "vscode",
            "tool_name": "run_in_terminal",
            "decision": "block",
            "trigger": "source-read",
            "command_preview": "grep foo bar",
            "policy_version": "terminal-guard-v1",
            "policy_source": "packages/workstate-system/scripts/hooks/terminal-guard.py",
        },
    )
    assert result["ok"] is True, result


def test_terminal_guard_telemetry_contract_has_replay_variant() -> None:
    from workstate_handoff_mcp.write_contracts import get_write_contract

    contract = get_write_contract("terminal_guard_telemetry")
    assert contract is not None
    assert "replay" in contract.variants
    replay_variant = contract.variants["replay"]
    assert "spool_path" in replay_variant.optional


def test_validate_write_terminal_guard_telemetry_replay_accepts_spool_path() -> None:
    from workstate_handoff_mcp.write_contracts import validate_write

    result = validate_write(
        "terminal_guard_telemetry",
        {
            "operation": "replay",
            "spool_path": ".task-state/terminal_guard.jsonl",
        },
    )
    assert result["ok"] is True, result
