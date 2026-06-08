from __future__ import annotations

import json
import stat
from pathlib import Path


def write_fake_cli(target: Path, body: str) -> None:
    target.write_text(body)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def write_status_handoff_cli(
    target: Path,
    *,
    repo_path: str,
    task_ref: str,
    branch: str,
    task_plan_path: str,
    blockers_count: int | None = None,
    findings_high: int | None = None,
    findings_medium: int | None = None,
    findings_low: int | None = None,
    latest_test: dict[str, object] | None = None,
    fail_findings: bool = False,
    fail_blockers: bool = False,
    fail_verified_tests: bool = False,
) -> None:
    findings_payload = json.dumps(
        {
            "ok": True,
            "data": {
                "counts": {
                    "severity": {
                        "high": findings_high or 0,
                        "medium": findings_medium or 0,
                        "low": findings_low or 0,
                    }
                }
            },
        }
    )
    identity_payload = json.dumps(
        {
            "ok": True,
            "data": {
                "active": {
                    "task_ref": task_ref,
                    "status": "in_progress",
                    "target_branch": branch,
                    "target_worktree_path": repo_path,
                    "task_plan_path": task_plan_path,
                },
                "limits": {},
            },
        }
    )
    blockers_payload = json.dumps(
        {
            "ok": True,
            "data": {
                "blockers_open": [{"id": idx} for idx in range(blockers_count or 0)],
                "active": {},
                "limits": {},
            },
        }
    )
    verified_tests_payload = json.dumps(
        {
            "ok": True,
            "data": {
                "tests": [] if latest_test is None else [latest_test],
                "total_matching": 0 if latest_test is None else 1,
                "returned": 0 if latest_test is None else 1,
                "has_more": False,
            },
        }
    )
    write_fake_cli(
        target,
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "argv = sys.argv[1:]\n"
        "if 'review-findings' in argv:\n"
        + (
            "    print('tool unavailable', file=sys.stderr)\n    sys.exit(2)\n"
            if fail_findings
            else f"    print({findings_payload!r})\n"
        )
        + "elif 'state' in argv and 'identity' in argv:\n"
        + f"    print({identity_payload!r})\n"
        + "elif 'state' in argv and 'blockers_open' in argv:\n"
        + (
            "    print('tool unavailable', file=sys.stderr)\n    sys.exit(2)\n"
            if fail_blockers
            else f"    print({blockers_payload!r})\n"
        )
        + "elif 'get-verified-tests' in argv:\n"
        + (
            "    print('tool unavailable', file=sys.stderr)\n    sys.exit(2)\n"
            if fail_verified_tests
            else f"    print({verified_tests_payload!r})\n"
        )
        + "else:\n"
        + "    print(json.dumps({'ok': False, 'argv': argv}))\n",
    )


def write_tasks_handoff_cli(
    target: Path,
    *,
    rows: list[dict[str, object]] | None = None,
    fail_rows: bool = False,
    malformed_rows: bool = False,
    unsupported_rows: bool = False,
    delay_seconds: float | None = None,
) -> None:
    rows_payload = json.dumps([] if rows is None else rows)
    write_fake_cli(
        target,
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import time\n"
        "import sys\n"
        "argv = sys.argv[1:]\n"
        + (f"time.sleep({delay_seconds!r})\n" if delay_seconds is not None else "")
        + "if 'handoff-rows' in argv:\n"
        + (
            "    print(\"invalid choice: 'handoff-rows'\", file=sys.stderr)\n    sys.exit(2)\n"
            if unsupported_rows
            else "    print('tool unavailable', file=sys.stderr)\n    sys.exit(2)\n"
            if fail_rows
            else "    print('{broken json')\n"
            if malformed_rows
            else f"    print({rows_payload!r})\n"
        )
        + "else:\n"
        + "    print(json.dumps({'ok': False, 'argv': argv}))\n",
    )