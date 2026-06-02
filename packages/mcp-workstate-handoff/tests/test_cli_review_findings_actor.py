"""WORKSTATE-REF-02 implementation note — CLI actor channel for `review-findings` resolve / update.

These tests pin the argparse shape, payload projection, and end-to-end
behavior for `--actor-commit-sha` / `--actor-branch` on the `resolve` and
`update` operations. WORKSTATE-REF-51 implementation note added the same channel to `set`; this
slice mirrors it onto the remaining write operations so callers that already
know the commit they want to attribute (CI / scripts resolving from a known
sha) can bypass automatic git-context detection without importing the Python
API.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from workstate_handoff_mcp import api, cli
from workstate_handoff_mcp.review_findings_updates import WorkspaceCleanliness


def _parse_response(raw: str | dict) -> dict:
    result = raw if isinstance(raw, dict) else json.loads(raw)
    if isinstance(result, dict) and result.get("schema_version") == 2:
        data = result.get("data", {})
        flat = {**result, **data}
        return flat
    return result


def _run_cli(argv: list[str], capsys) -> dict:
    if "--workspace-root" in argv and "--state-dir" not in argv:
        ws_idx = argv.index("--workspace-root")
        if ws_idx + 1 < len(argv):
            ws_path = Path(argv[ws_idx + 1])
            argv = list(argv)
            argv.insert(ws_idx + 2, str(ws_path / ".task-state"))
            argv.insert(ws_idx + 2, "--state-dir")
    original_argv = sys.argv
    sys.argv = argv
    try:
        cli.main()
    finally:
        sys.argv = original_argv
    return _parse_response(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# Argparse shape
# ---------------------------------------------------------------------------


def test_review_findings_parser_exposes_actor_flags_for_resolve() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(
        [
            "review-findings",
            "--operation",
            "resolve",
            "--resolve-finding-id",
            "X-1",
            "--actor-commit-sha",
            "deadbeefcafef00d1234567890abcdef12345678",
            "--actor-branch",
            "feature/x",
        ]
    )
    assert args.operation == "resolve"
    assert args.actor_commit_sha == "deadbeefcafef00d1234567890abcdef12345678"
    assert args.actor_branch == "feature/x"


def test_review_findings_parser_exposes_actor_flags_for_update() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(
        [
            "review-findings",
            "--operation",
            "update",
            "--status",
            "fixed",
            "--finding-id",
            "X-1",
            "--actor-commit-sha",
            "deadbeefcafef00d1234567890abcdef12345678",
            "--actor-branch",
            "feature/x",
        ]
    )
    assert args.operation == "update"
    assert args.actor_commit_sha == "deadbeefcafef00d1234567890abcdef12345678"
    assert args.actor_branch == "feature/x"


# ---------------------------------------------------------------------------
# Payload projection — _dispatch_review_findings puts actor into payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra_argv",
    [
        ["--operation", "resolve", "--resolve-finding-id", "X-1"],
        ["--operation", "update", "--status", "fixed", "--finding-id", "X-1"],
    ],
)
def test_dispatch_projects_actor_into_payload(extra_argv: list[str]) -> None:
    parser = cli._build_parser()
    args = parser.parse_args(
        [
            "review-findings",
            *extra_argv,
            "--actor-commit-sha",
            "deadbeefcafef00d1234567890abcdef12345678",
            "--actor-branch",
            "feature/x",
        ]
    )
    captured: dict = {}

    def _capture(*, review):
        captured["review"] = review
        return {"ok": True}

    with mock.patch.object(cli, "review_findings", _capture):
        cli._dispatch_review_findings(args)

    review = captured["review"]
    assert review["operation"] == args.operation
    assert review["actor"] == {
        "commit_sha": "deadbeefcafef00d1234567890abcdef12345678",
        "branch": "feature/x",
    }


def test_dispatch_omits_actor_when_flags_absent() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(
        [
            "review-findings",
            "--operation",
            "resolve",
            "--resolve-finding-id",
            "X-1",
        ]
    )
    captured: dict = {}

    def _capture(*, review):
        captured["review"] = review
        return {"ok": True}

    with mock.patch.object(cli, "review_findings", _capture):
        cli._dispatch_review_findings(args)

    assert "actor" not in captured["review"], (
        "Empty actor dict must not be projected; the resolver falls back to "
        "git-context detection when the CLI caller omits both flags."
    )


# ---------------------------------------------------------------------------
# End-to-end resolve with explicit actor
# ---------------------------------------------------------------------------


def test_resolve_cli_with_explicit_actor_writes_receipt(tmp_path: Path, capsys, monkeypatch) -> None:
    """End-to-end: `review-findings --operation resolve --actor-commit-sha … --actor-branch …`
    writes the explicit values into the receipt's `workspace_commit_sha` /
    `workspace_branch`, regardless of the test process's git HEAD.
    """
    explicit_sha = "deadbeefcafef00d1234567890abcdef12345678"
    explicit_branch = "feature/WORKSTATE-02-actor"

    api.configure_runtime(api.RuntimeConfig.for_workspace(tmp_path))
    _parse_response(api.set_handoff_state(task_ref="WORKSTATE02-actor-task", objective="explicit actor"))
    _parse_response(
        api.record_review_finding(
            session="cli",
            finding_id="W-1",
            severity="medium",
            file_path="README.md",
            description="actor channel smoke",
            actor={"agent": "reviewer", "commit_sha": explicit_sha},
        )
    )

    monkeypatch.setattr(
        "workstate_handoff_mcp.review_findings_updates._workspace_has_uncommitted_changes",
        lambda *a, **k: WorkspaceCleanliness(False),
    )
    monkeypatch.setattr(
        "workstate_handoff_mcp.review_findings_updates._classify_commit_relation",
        lambda reference_sha, candidate_sha: "same" if reference_sha == candidate_sha else "unknown",
    )

    payload = _run_cli(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(tmp_path),
            "review-findings",
            "--operation",
            "resolve",
            "--task-ref",
            "WORKSTATE02-actor-task",
            "--resolve-finding-id",
            "W-1",
            "--session",
            "cli-resolve",
            "--actor-commit-sha",
            explicit_sha,
            "--actor-branch",
            explicit_branch,
        ],
        capsys,
    )

    assert payload["ok"] is True
    receipt = payload["receipt"]
    assert receipt["workspace_commit_sha"] == explicit_sha
    assert receipt["workspace_branch"] == explicit_branch
    assert receipt["counts"]["fixed"] == 1
    assert receipt["results"][0]["finding_id"] == "W-1"
    assert receipt["results"][0]["outcome"] == "fixed"
