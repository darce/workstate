"""Review-parallel scaffold guards plus coordinator runtime proof.

These tests cover both the static workflow surface and the coordinator-side
runtime contract promised by the workflow plan:

1. ``portable_commands.json`` exposes ``/review-parallel`` with the
   argument schema the plan requires — ``reviewers_count`` and
   ``reviewer_prompt_template`` only, and explicitly NO ``merge_strategy``
    knob (resolved planning finding DEMO-PLAN-09).
2. ``.claude/skills/review-parallel/SKILL.md`` exists and carries the
   harness-routing table rows the plan requires (Claude Code / Codex /
   Copilot / external orchestrator) plus the explicit prohibition on
   calling ``ClaudeCodeAdapter`` from inside an active Claude Code
    coordinator session (resolved planning finding DEMO-PLAN-05).
3. Reviewer prompt templates live at the harness-neutral path
   ``config/agent-workflows/prompts/review-parallel/`` so the Claude,
   VS Code, and Codex adapters all resolve them (resolved planning
    finding DEMO-PLAN-10).
4. The implementation note baseline fixture
    ``packages/mcp-workstate-orchestrator/tests/fixtures/review_baseline.json``
   exists and declares the schema the token-envelope assertion consumes.
5. A coordinator-level runtime contract test seeds reviewer-scoped task_refs,
   merges them into the coordinator task, and proves ``merged_from``
    provenance plus additive source-row preservation.
6. Coordinator-side envelope cost stays below half the recorded serial
   baseline using a deterministic serialized-payload proxy. There is no
   public turn-metrics recorder on this workflow surface yet, so the test
   measures the merge/list payloads the coordinator itself handles.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_ROOT = PACKAGE_ROOT / "workstate_system" / "payload"
MONOREPO_ROOT = PACKAGE_ROOT.parent.parent
MANIFEST_PATH = PAYLOAD_ROOT / "config" / "agent-workflows" / "portable_commands.json"
# implementation note step 1: canonical skill source is the neutral layout
# (skill.yaml + body.md). Content assertions target body.md.
SKILL_PATH = PAYLOAD_ROOT / "skills" / "review-parallel" / "body.md"
SKILL_YAML_PATH = PAYLOAD_ROOT / "skills" / "review-parallel" / "skill.yaml"
PROMPT_DIR = PAYLOAD_ROOT / "config" / "agent-workflows" / "prompts" / "review-parallel"
BASELINE_PATH = (
    MONOREPO_ROOT
    / "packages"
    / "mcp-workstate-orchestrator"
    / "tests"
    / "fixtures"
    / "review_baseline.json"
)


def _parse(raw: str | dict) -> dict:
    result = raw if isinstance(raw, dict) else json.loads(raw)
    if isinstance(result, dict) and result.get("schema_version") == 2:
        data = result.get("data", {})
        scope = result.get("scope", {})
        flat = {**result, **data}
        if "task_ref" not in flat and scope.get("task_ref"):
            flat["task_ref"] = scope["task_ref"]
        return flat
    return result


def _approx_tokens(payload: dict) -> int:
    serialized = json.dumps(payload, sort_keys=True).encode("utf-8")
    return max(1, math.ceil(len(serialized) / 4))


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


@pytest.fixture(scope="module")
def review_parallel_command(manifest: dict) -> dict:
    for entry in manifest["commands"]:
        if entry["command_id"] == "review-parallel":
            return entry
    pytest.fail("review-parallel entry missing from portable_commands.json")


@pytest.fixture()
def isolated_handoff(tmp_path: Path) -> dict:
    from workstate_handoff_mcp import RuntimeConfig, configure_runtime

    state_dir = tmp_path / ".task-state"
    current_task_path = tmp_path / "CURRENT_TASK.json"
    runtime = RuntimeConfig.for_workspace(
        tmp_path, state_dir=state_dir, current_task_path=current_task_path
    )
    configure_runtime(runtime)
    return {
        "state_dir": state_dir,
        "current_task_path": current_task_path,
    }


def _seed_reviewer_findings(
    task_ref: str, session: str, finding_prefix: str
) -> list[str]:
    from workstate_handoff_mcp import batch_record_review_findings, set_handoff_state

    finding_ids = [f"{finding_prefix}-{idx}" for idx in range(1, 4)]
    _parse(
        set_handoff_state(
            task_ref=task_ref,
            objective=f"Reviewer scope for {task_ref}",
            status="in_progress",
        )
    )
    _parse(
        batch_record_review_findings(
            session=session,
            task_ref=task_ref,
            findings=[
                {
                    "finding_id": finding_id,
                    "severity": "medium",
                    "file_path": f"reviews/{task_ref.lower()}/{finding_id.lower()}.md",
                    "description": f"Synthetic reviewer finding {finding_id}",
                    "review_mode": "branch",
                }
                for finding_id in finding_ids
            ],
        )
    )
    return finding_ids


def test_review_parallel_registered_in_manifest(review_parallel_command: dict) -> None:
    assert review_parallel_command["skill"] == "review-parallel"
    assert "review_findings" in review_parallel_command["description"].lower() or (
        "parallel" in review_parallel_command["description"].lower()
    )


def test_review_parallel_argument_schema_matches_plan(
    review_parallel_command: dict,
) -> None:
    names = {arg["name"] for arg in review_parallel_command["argument_schema"]}
    assert "reviewers-count" in names or "reviewers_count" in names, names
    assert "reviewer-prompt-template" in names or "reviewer_prompt_template" in names, (
        names
    )
    assert "merge-strategy" not in names and "merge_strategy" not in names, (
        "merge_strategy must not be re-introduced — see DEMO-PLAN-09. "
        "`review_findings(operation='merge')` is an unconditional union."
    )


def test_review_parallel_skill_exists() -> None:
    assert SKILL_PATH.is_file(), f"missing skill: {SKILL_PATH}"


def test_review_parallel_skill_declares_harness_routing() -> None:
    content = SKILL_PATH.read_text()
    required_markers = (
        "Claude Code",
        "Codex",
        "Copilot",
        "Agent` tool",
        "run_structured_turn",
        "BackendAdapter",
    )
    missing = [marker for marker in required_markers if marker not in content]
    assert not missing, f"SKILL.md missing required harness-routing markers: {missing}"


def test_review_parallel_skill_declares_capability_fallback_contract() -> None:
    """implementation note D1: the skill must declare a deterministic degradation
    contract — probe available backends (``list_available_backends``), then
    fall back to in-process reviewers and record the downgrade
    (``routing_downgrade``) — so every runtime converges on the same recorded
    shape instead of ad-hoc per-run improvisation when the preferred backend
    is unavailable."""
    content = SKILL_PATH.read_text()
    heading = "### Capability detection & deterministic degradation contract"
    assert heading in content, f"missing contract heading: {heading}"
    # Assert the markers are co-located *within* the contract section (up to the
    # next heading), so the guard tracks the contract, not stray mentions.
    section = content.split(heading, 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]
    required_markers = (
        "list_available_backends",
        "routing_downgrade",
        "in-process",
        "fallback",
    )
    missing = [m for m in required_markers if m not in section]
    assert not missing, f"contract section missing markers: {missing}"


def test_review_parallel_skill_forbids_claude_cli_inside_claude_coordinator() -> None:
    content = SKILL_PATH.read_text()
    assert "ClaudeCodeAdapter" in content, (
        "SKILL.md must mention ClaudeCodeAdapter when documenting the forbidden "
        "CLI-subprocess path — see DEMO-PLAN-05."
    )
    lowered = content.lower()
    assert "forbid" in lowered or "must not" in lowered or "do not" in lowered, (
        "SKILL.md must explicitly forbid invoking the claude CLI adapter from "
        "inside an active Claude Code coordinator session."
    )


def test_reviewer_prompt_template_directory_exists() -> None:
    assert PROMPT_DIR.is_dir(), f"missing harness-neutral prompt dir: {PROMPT_DIR}"
    markdown_templates = sorted(PROMPT_DIR.glob("*.md"))
    assert markdown_templates, f"no reviewer prompt templates under {PROMPT_DIR}"


def test_baseline_fixture_declares_expected_schema() -> None:
    assert BASELINE_PATH.is_file(), f"missing baseline fixture: {BASELINE_PATH}"
    payload = json.loads(BASELINE_PATH.read_text())
    required_keys = {
        "schema_version",
        "captured_at",
        "captured_by",
        "fixture_diff_lines",
        "serial_branch_review_total_tokens",
        "identity_response_bytes",
        "identity_variable_bytes",
        "measurement_method",
    }
    missing = required_keys - set(payload)
    assert not missing, f"baseline fixture missing required keys: {missing}"
    assert payload["schema_version"] == 1, payload["schema_version"]
    assert isinstance(payload["measurement_method"], str)
    assert payload["fixture_diff_lines"] >= 1


def test_review_parallel_runtime_merge_contract_uses_scoped_reviewer_task_refs(
    isolated_handoff: dict,
) -> None:
    from workstate_handoff_mcp import review_findings, set_handoff_state

    coordinator_task = "DEMO-REVIEW-COORD"
    reviewer_a = f"{coordinator_task}-REV-A"
    reviewer_b = f"{coordinator_task}-REV-B"
    reviewer_a_ids = _seed_reviewer_findings(reviewer_a, "reviewer-a-pass", "A")
    reviewer_b_ids = _seed_reviewer_findings(reviewer_b, "reviewer-b-pass", "B")
    _parse(
        set_handoff_state(
            task_ref=coordinator_task,
            objective="Coordinator scope",
            status="in_progress",
        )
    )

    merge_result = _parse(
        review_findings(
            review={
                "operation": "merge",
                "source_task_refs": [reviewer_a, reviewer_b],
                "target_task_ref": coordinator_task,
                "session": "coord-merge-pass",
            }
        )
    )
    assert merge_result["ok"] is True, merge_result
    assert merge_result["task_ref"] == coordinator_task
    assert merge_result["written"] == len(reviewer_a_ids) + len(reviewer_b_ids)

    merged_rows = _parse(
        review_findings(
            review={
                "operation": "list",
                "task_ref": coordinator_task,
                "status": "open",
                "limit": 20,
            }
        )
    )
    assert merged_rows["total_matching"] == 6

    expected_sources = {
        **{finding_id: reviewer_a for finding_id in reviewer_a_ids},
        **{finding_id: reviewer_b for finding_id in reviewer_b_ids},
    }
    for finding in merged_rows["findings"]:
        merged_from = finding.get("merged_from")
        assert merged_from is not None, finding
        assert merged_from["task_ref"] == expected_sources[finding["finding_id"]]
        assert merged_from["finding_id"] == finding["finding_id"]

    reviewer_a_rows = _parse(
        review_findings(
            review={"operation": "list", "task_ref": reviewer_a, "limit": 10}
        )
    )
    reviewer_b_rows = _parse(
        review_findings(
            review={"operation": "list", "task_ref": reviewer_b, "limit": 10}
        )
    )
    assert reviewer_a_rows["total_matching"] == 3
    assert reviewer_b_rows["total_matching"] == 3
    for finding in reviewer_a_rows["findings"] + reviewer_b_rows["findings"]:
        assert "merged_from" not in finding


def test_review_parallel_runtime_coordinator_envelope_stays_under_half_baseline(
    isolated_handoff: dict,
) -> None:
    from workstate_handoff_mcp import review_findings, set_handoff_state

    baseline = json.loads(BASELINE_PATH.read_text())
    coordinator_task = "DEMO-REVIEW-COORD-BUDGET"
    reviewer_a = f"{coordinator_task}-REV-A"
    reviewer_b = f"{coordinator_task}-REV-B"
    _seed_reviewer_findings(reviewer_a, "reviewer-a-budget", "A")
    _seed_reviewer_findings(reviewer_b, "reviewer-b-budget", "B")
    _parse(
        set_handoff_state(
            task_ref=coordinator_task,
            objective="Coordinator budget scope",
            status="in_progress",
        )
    )

    merge_result = _parse(
        review_findings(
            review={
                "operation": "merge",
                "source_task_refs": [reviewer_a, reviewer_b],
                "target_task_ref": coordinator_task,
                "session": "coord-budget-pass",
            }
        )
    )
    merged_rows = _parse(
        review_findings(
            review={
                "operation": "list",
                "task_ref": coordinator_task,
                "status": "open",
                "limit": 20,
                "detail": "summary",
            }
        )
    )

    coordinator_proxy_tokens = _approx_tokens(merge_result) + _approx_tokens(
        merged_rows
    )
    half_baseline = int(baseline["serial_branch_review_total_tokens"] * 0.5)
    assert coordinator_proxy_tokens <= half_baseline, (
        f"Coordinator envelope proxy {coordinator_proxy_tokens} tokens exceeds "
        f"half-baseline ceiling {half_baseline}. The merge/list payloads should stay "
        f"well below the recorded serial /branch-review cost."
    )


def test_identity_variable_payload_stays_within_baseline_ceiling(
    tmp_path: Path,
) -> None:
    """DEMO-BR-03 (identity portion): the /auto-fix bounded-read contract
    guards the identity payload's variable data, excluding the static limits
    registry that is identical across task refs. Runtime assertion against a
    freshly-initialised task_ref so the test is hermetic and does not depend on
    live DB size."""
    from workstate_handoff_mcp import (
        RuntimeConfig,
        configure_runtime,
        get_handoff_state,
        set_handoff_state,
    )

    configure_runtime(RuntimeConfig.for_repo(tmp_path))
    set_handoff_state(
        task_ref="BASELINE-PROBE",
        objective="Probe identity response size for DEMO-BR-03 ceiling assertion.",
        status="in_progress",
    )
    resp = get_handoff_state(task_ref="BASELINE-PROBE", sections="identity")
    data = resp["data"] if isinstance(resp, dict) and "data" in resp else resp
    variable_data = {key: value for key, value in data.items() if key != "limits"}
    actual_bytes = len(json.dumps(variable_data).encode("utf-8"))

    payload = json.loads(BASELINE_PATH.read_text())
    ceiling = int(payload["identity_variable_bytes"] * 1.10)
    assert actual_bytes <= ceiling, (
        f"identity variable payload {actual_bytes}B exceeds baseline ceiling {ceiling}B "
        f"(baseline={payload['identity_variable_bytes']}B + 10%). The bounded-read "
        f"contract for /auto-fix guards live identity growth separately from static limits."
    )
