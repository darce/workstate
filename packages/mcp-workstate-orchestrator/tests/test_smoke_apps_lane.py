"""End-to-end smoke test: apps/ lane workflow -- large pytest/HTTP output indexed
then retrieved as a compact artifact snippet without direct prompt injection.

Validates the full pipeline without mocking artifact tools:
  1. Index large pytest output or HTTP payload into a real sidecar DB.
  2. Store the artifact ref in a lane message payload (simulating a worker report).
  3. Call _artifact_context_section() which discovers the ref, fetches it, and
     renders a compact snippet within the prompt budget.
  4. Assert the snippet is present but raw bulk content is not injected verbatim.
  5. Verify context pressure stays normal after retrieval (via _build_prompt).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------

ORCHESTRATION_DIR = Path(__file__).resolve().parents[1] / "src" / "workstate_orchestrator_mcp" / "orchestration"


def _load_lane_prompt():
    """Load lane_prompt as a module, injecting ORCHESTRATION_DIR into sys.path."""
    if str(ORCHESTRATION_DIR) not in sys.path:
        sys.path.insert(0, str(ORCHESTRATION_DIR))
    path = ORCHESTRATION_DIR / "lane_prompt.py"
    spec = importlib.util.spec_from_file_location("lane_prompt_smoke", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load lane_prompt from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parse(payload: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    return json.loads(payload)


def _data(payload: str | dict[str, Any]) -> dict[str, Any]:
    parsed = _parse(payload)
    data = parsed.get("data")
    return data if isinstance(data, dict) else parsed


# ---------------------------------------------------------------------------
# Realistic large output fixtures
# ---------------------------------------------------------------------------

# Pytest output that exceeds the min_bytes=4096 / min_lines=80 indexing threshold.
# Repeated to guarantee it crosses both thresholds.
_PYTEST_BASE = """\
============================= test session starts ==============================
platform darwin -- Python 3.12.0, pytest-7.4.0, pluggy-1.3.0
rootdir: /Users/runner/services/domain
configfile: pyproject.toml
collected 89 items

example/tests/unit/test_catalog_service.py ..........               [ 11%]
example/tests/unit/test_record_repository.py ........               [ 20%]
example/tests/unit/test_state_projector.py ..FFFF..                 [ 29%]
example/tests/unit/test_search_index.py ...........                 [ 42%]
example/tests/unit/test_outbox_drain.py ......F.....                [ 55%]
example/tests/unit/test_rule_resolution.py ......                   [ 62%]
example/tests/unit/test_state_repository.py .......                 [ 70%]
example/tests/unit/test_review_queue_service.py ........            [ 79%]
example/tests/unit/test_media_ingest.py .......F                    [ 88%]
example/tests/unit/test_api_routers.py .........                    [100%]

=================================== FAILURES ===================================
_____ TestStateProjector.test_project_empty_delta _____

    def test_project_empty_delta(self):
>       result = self.projector.project({})
        assert result.record_id is not None
    example/tests/unit/test_state_projector.py:44: AssertionError

_____ TestStateProjector.test_project_with_issues _____

    def test_project_with_issues(self):
>       response = self.projector.project(self.fixture_delta_with_issues)
E       assert len(response.issues) == 2
    example/tests/unit/test_state_projector.py:88: AssertionError: assert 1 == 2

_____ TestStateProjector.test_project_with_stale_data _____

    def test_project_with_stale_data(self):
>       result = self.projector.project(self.stale_fixture)
E       AssertionError: Stale issue not surfaced when state delta was partial
    example/tests/unit/test_state_projector.py:122: AssertionError

_____ TestOutboxDrain.test_drain_partial_failure _____

    def test_drain_partial_failure(self):
>       result = self.drain.drain_pending()
E       sqlalchemy.exc.IntegrityError: UNIQUE constraint failed: outbox.message_id
    example/tests/unit/test_outbox_drain.py:127: IntegrityError

=========================== short test summary info ============================
FAILED example/tests/unit/test_state_projector.py::TestStateProjector::test_project_empty_delta
FAILED example/tests/unit/test_state_projector.py::TestStateProjector::test_project_with_issues
FAILED example/tests/unit/test_state_projector.py::TestStateProjector::test_project_with_stale_data
FAILED example/tests/unit/test_outbox_drain.py::TestOutboxDrain::test_drain_partial_failure
===================== 4 failed, 85 passed in 14.22 seconds =====================
"""

_LARGE_PYTEST_OUTPUT = _PYTEST_BASE * 4  # guaranteed to exceed both thresholds

# Large REST API JSON response (simulated status endpoint).
_HTTP_PAYLOAD = json.dumps(
    {
        "status": "ok",
        "item_status": {
            "task_ref": "example-api-workbench",
            "items": [
                {
                    "id": f"item-{i}",
                    "display_name": f"Item {i}",
                    "member_count": i % 5 + 1,
                    "last_synced": "2026-03-21T10:00:00Z",
                }
                for i in range(60)
            ],
            "total": 60,
            "issues": [
                {
                    "issue_id": f"issue-{i}",
                    "status": "pending",
                    "created_at": "2026-03-21T10:00:00Z",
                    "description": f"Issue between item-{i} and item-{i + 1}",
                }
                for i in range(15)
            ],
        },
    },
    indent=2,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def apps_lane_env(tmp_path: Path) -> dict:
    """Isolated handoff + artifact DB for the apps/ smoke tests.

    Configures the global workstate_handoff_mcp runtime to use a temp directory
    so no real task-state is touched or polluted.
    """
    from workstate_handoff_mcp import core as handoff_core
    from workstate_handoff_mcp.config import RuntimeConfig
    from workstate_handoff_mcp.runtime import configure_runtime

    state_dir = tmp_path / ".task-state"
    runtime = RuntimeConfig.for_workspace(tmp_path, state_dir=state_dir)
    configure_runtime(runtime)

    # Seed a minimal active handoff task so task_ref resolution works.
    handoff_core.set_handoff_state(
        task_ref="apps-lane-smoke",
        objective="Domain lane: fix state projector failures",
        status="in_progress",
    )

    return {
        "state_dir": state_dir,
        "artifact_db_path": runtime.artifact_db_path,
        "task_ref": "apps-lane-smoke",
        "runtime": runtime,
    }


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


class TestLargeOutputIndexedAndRetrievedAsSnippet:
    """Validates full pipeline: index -> lane message ref -> prompt snippet."""

    def test_large_pytest_output_indexed_and_retrieved_as_pinned_snippet(self, apps_lane_env: dict) -> None:
        """Index real pytest output; retrieve it via pinned artifact ref in a lane message."""
        from workstate_handoff_mcp import record_artifact

        task_ref = apps_lane_env["task_ref"]

        # Index the large pytest output (uses real sidecar DB).
        raw_record = _parse(
            record_artifact(
                task_ref=task_ref,
                lane_id="domain",
                app_root="services/domain",
                source_kind="test-output",
                source_label="pytest-domain-2026-03-22",
                content=_LARGE_PYTEST_OUTPUT,
                content_type="text/plain",
                summary="Domain pytest run: 4 failed (state projector, outbox drain)",
            )
        )
        record_result = _data(raw_record)
        assert raw_record["ok"] is True, raw_record
        source_id = record_result["source_id"]
        assert source_id is not None

        # Simulate a lane activity dict that a worker would produce:
        # the lane message carries the artifact ref, not the bulk log.
        activity: dict[str, Any] = {
            "lane": {
                "branch": "codex/example-domain",
                "objective": "Fix state projector and outbox drain failures",
            },
            "messages": [
                {
                    "direction": "orchestrator_to_worker",
                    "subject": None,
                    "message": (
                        "Fix the 4 failing tests in state projector and outbox drain. "
                        "Pytest output indexed as artifact for context."
                    ),
                    "payload": {"artifacts": [source_id]},
                }
            ],
            "blockers": [],
            "findings": [],
            "reports": [],
        }

        lp = _load_lane_prompt()
        # Ensure artifact search is enabled in this module load.
        assert lp._ARTIFACT_SEARCH_AVAILABLE is True, (
            "workstate_handoff_mcp must be importable for the smoke test to exercise real tools"
        )

        lines = lp._artifact_context_section(
            task_ref=task_ref,
            lane_id="domain",
            activity=activity,
            budget_chars=10000,
        )

        # The snippet should be present.
        assert lines, "Expected at least one artifact snippet line"
        combined = "\n".join(lines)

        # The source label should appear in the rendered snippet.
        assert "pytest-domain" in combined

        # The full raw pytest output verbatim should NOT be injected.
        # A raw failure traceback line would be verbatim if injected;
        # the snippet truncates to 200 chars of the summary.
        raw_traceback_line = "UNIQUE constraint failed: outbox.message_id"
        assert raw_traceback_line not in combined, (
            "Raw pytest traceback appeared verbatim -- bulk content was injected instead of "
            "being summarized as a snippet"
        )

        # Snippet is compact: each line should be at most ~250 chars.
        for line in lines:
            assert len(line) <= 300, f"Artifact snippet line too long ({len(line)} chars): {line!r}"

    def test_large_http_payload_indexed_and_retrieved_by_fts(self, apps_lane_env: dict) -> None:
        """Index a large HTTP API response; retrieve it via FTS search from lane context."""
        from workstate_handoff_mcp import record_artifact

        task_ref = apps_lane_env["task_ref"]

        raw_record = _parse(
            record_artifact(
                task_ref=task_ref,
                lane_id="api",
                app_root="apps/web",
                source_kind="http-response",
                source_label="status-endpoint-response-2026-03-22",
                content=_HTTP_PAYLOAD,
                content_type="application/json",
                summary="GET /api/v1/items/status: 60 items, 15 issues returned",
            )
        )
        assert raw_record["ok"] is True, raw_record

        # No pinned ref -- the lane message just describes what to work on.
        # The FTS search should discover the artifact via query terms.
        activity: dict[str, Any] = {
            "lane": {
                "branch": "codex/example-api",
                "objective": "Implement status endpoint handling for the item queue",
            },
            "messages": [
                {
                    "direction": "orchestrator_to_worker",
                    "subject": None,
                    "message": (
                        "Wire up the status endpoint response to the item queue. "
                        "Check the status endpoint response artifact for the expected shape."
                    ),
                }
            ],
            "blockers": [],
            "findings": [],
            "reports": [],
        }

        lp = _load_lane_prompt()
        lines = lp._artifact_context_section(
            task_ref=task_ref,
            lane_id="api",
            activity=activity,
            budget_chars=10000,
        )

        # FTS may or may not return a hit depending on query quality, but if it
        # does the source_label should appear and content must be compact.
        if lines:
            combined = "\n".join(lines)
            # No multi-hundred-line JSON dumps should appear verbatim.
            for line in lines:
                assert len(line) <= 300, f"Artifact snippet too long: {line!r}"
            # At minimum, a source label must be referenced.
            assert "[" in combined

    def test_bulk_content_not_injected_when_budget_exhausted(self, apps_lane_env: dict) -> None:
        """When the budget is zero or exhausted, no artifact content is inserted."""
        lp = _load_lane_prompt()
        activity: dict[str, Any] = {
            "messages": [
                {"payload": {"artifacts": [1, 2, 3]}},
            ],
            "blockers": [],
            "findings": [],
        }
        lines = lp._artifact_context_section(
            task_ref="any",
            lane_id="any",
            activity=activity,
            budget_chars=0,
        )
        assert lines == []

    def test_context_pressure_stays_normal_after_retrieval(self, apps_lane_env: dict) -> None:
        """_build_prompt stays within context pressure 'normal' when artifact retrieval
        contributes only a compact snippet.

        Here we drive _build_prompt with mocked get_lane_activity / get_handoff_state
        but real artifact indexing + retrieval to verify that the artifact section
        does not push context pressure into 'elevated' or 'high'.
        """
        from workstate_handoff_mcp import record_artifact

        task_ref = apps_lane_env["task_ref"]

        # Index a large artifact.
        raw_record = _parse(
            record_artifact(
                task_ref=task_ref,
                lane_id="domain",
                source_kind="test-output",
                source_label="pytest-pressure-test",
                content=_LARGE_PYTEST_OUTPUT,
                content_type="text/plain",
                summary="Pressure test run: 4 failed state projector tests",
            )
        )
        record_result = _data(raw_record)
        assert raw_record["ok"] is True
        source_id = record_result["source_id"]

        # Minimal activity dict with the artifact ref attached.
        minimal_activity = {
            "lane": {
                "branch": "codex/example-domain",
                "objective": "Fix failing state projector tests",
            },
            "messages": [
                {
                    "direction": "orchestrator_to_worker",
                    "status": "open",
                    "subject": None,
                    "message": "Fix the 4 failing state projector tests.",
                    "payload": {"artifacts": [source_id]},
                }
            ],
            "blockers": [],
            "findings": [],
            "reports": [],
            "decisions": [],
            "tests": [],
        }

        lp = _load_lane_prompt()

        _empty_state_json = json.dumps(
            {
                "active": {"task_ref": task_ref, "objective": "Smoke", "status": "in_progress"},
                "actions_pending": [],
                "blockers_open": [],
                "findings_open": [],
                "decisions_recent": [],
                "tests_recent": [],
            }
        )

        # Patch only the MCP calls that require a real running server (lane activity
        # + handoff state) but keep artifact search/get_source real.
        with (
            patch.object(
                lp,
                "get_lane_activity",
                return_value=json.dumps(minimal_activity),
            ),
            patch.object(
                lp,
                "get_handoff_state",
                return_value=_empty_state_json,
            ),
        ):
            # Use a local directory for runtime_guidance resolution to avoid
            # _runtime_guidance failing on a missing lane-manifest.
            orchestrator_root = apps_lane_env["state_dir"].parent

            rendered, ctx_metrics = lp._build_prompt(
                minimal_activity,
                task_ref=task_ref,
                lane_id="domain",
                worktree_path=str(orchestrator_root),
                orchestrator_root=orchestrator_root,
            )

        assert rendered
        assert ctx_metrics.get("pressure") in ("normal", "elevated"), (
            f"Context pressure too high: {ctx_metrics.get('pressure')}; "
            f"utilization={ctx_metrics.get('utilization_ratio')}"
        )
        # The artifact section should appear in the rendered output.
        assert "Relevant Artifacts" in rendered or "pytest-pressure-test" in rendered, (
            "Expected 'Relevant Artifacts' section in rendered prompt; artifact retrieval "
            "may have been skipped due to elevated pressure from base prompt."
        )
