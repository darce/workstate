"""implementation note S6: install receipts and pre-flight tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_install_profiles import fake_remote_with_lifecycle  # noqa: F401

from workstate_bootstrap.install_receipt import (
    InstallPreflightError,
    InstallReceipt,
    run_install_preflight,
)
from workstate_bootstrap.subcommands import _doctor_install_receipt_findings


def test_preflight_rejects_non_writable_target(tmp_path: Path) -> None:
    target = tmp_path / "consumer"
    target.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    with patch("os.access", return_value=False):
        with pytest.raises(InstallPreflightError, match="not writable"):
            run_install_preflight(
                target=target,
                source_root=source,
                profile="minimal",
                source_kind="package",
            )


def test_doctor_reads_failed_receipt_before_probing() -> None:
    manifest = {
        "install_steps": [
            {
                "step": "presync_local_mcp",
                "status": "failed",
                "reason": "uv sync failed",
                "failure_class": "system",
            }
        ]
    }
    findings = _doctor_install_receipt_findings(manifest)
    assert len(findings) == 1
    assert findings[0]["kind"] == "install_step_receipt"
    assert findings[0]["path"] == "presync_local_mcp"


def test_install_records_finalize_step(
    tmp_path: Path,
    fake_remote_with_lifecycle: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()

    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_lifecycle

    subprocess.run(
        ["git", "init", "--initial-branch=main", "-q"],
        cwd=str(target),
        check=True,
    )

    install(target=target, remote_url=url, remote_ref=ref, profile="minimal")
    manifest = json.loads((target / ".workstate-bootstrap.json").read_text())
    steps = manifest.get("install_steps")
    assert isinstance(steps, list)
    assert any(step.get("step") == "finalize_manifest" for step in steps)


def test_repair_updates_deferred_prewarm_receipt(tmp_path: Path) -> None:
    from workstate_bootstrap.subcommands import _repair_deferred_install_steps

    target = tmp_path / "consumer"
    target.mkdir()
    manifest = {
        "configs": [{"path": ".mcp.json", "action": "created"}],
        "install_steps": [
            {
                "step": "prewarm_uvx_mcp",
                "status": "deferred",
                "reason": "offline",
                "failure_class": "system",
            }
        ],
        "offline_latch": True,
    }
    (target / ".workstate-bootstrap.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    (target / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "workstate-handoff-mcp": {"command": "uvx", "args": ["x"]}
                }
            }
        )
    )
    servers = {"workstate-handoff-mcp": {"command": "uvx", "args": ["x"]}}

    with patch(
        "workstate_bootstrap.install._prewarm_uvx_mcp_envs",
        return_value=["mcp-workstate-handoff@0.1.0"],
    ):
        repaired = _repair_deferred_install_steps(target, manifest, servers)

    assert repaired
    on_disk = json.loads((target / ".workstate-bootstrap.json").read_text())
    prewarm = next(
        s for s in on_disk["install_steps"] if s["step"] == "prewarm_uvx_mcp"
    )
    assert prewarm["status"] == "ok"
    assert on_disk["prewarm_refs"] == ["mcp-workstate-handoff@0.1.0"]
    # attach_to_manifest omits offline_latch when False; repair mirrors that.
    assert "offline_latch" not in on_disk


def test_abort_snapshot_preserves_existing_manifest(tmp_path: Path) -> None:
    """An update-time abort must not clobber the surfaces/configs the previous
    install materialized — they are still on disk."""
    target = tmp_path / "consumer"
    target.mkdir()
    existing = {
        "schema_version": 2,
        "profile": "all",
        "surfaces": [{"path": "docs/workstate", "source": "shared"}],
        "configs": [{"path": ".mcp.json", "action": "created"}],
    }
    (target / ".workstate-bootstrap.json").write_text(
        json.dumps(existing, indent=2) + "\n"
    )

    receipt = InstallReceipt()
    receipt.failed(
        "presync_local_mcp",
        reason="uv sync failed",
        failure_class="system",
        criticality="abort",
    )
    receipt.write_abort_snapshot(target, profile="all", source_kind="git_overlay")

    on_disk = json.loads((target / ".workstate-bootstrap.json").read_text())
    assert on_disk["surfaces"] == existing["surfaces"]
    assert on_disk["configs"] == existing["configs"]
    assert any(
        step["step"] == "presync_local_mcp" and step["status"] == "failed"
        for step in on_disk["install_steps"]
    )


def test_abort_snapshot_builds_fresh_manifest_when_absent(tmp_path: Path) -> None:
    target = tmp_path / "consumer"
    target.mkdir()

    receipt = InstallReceipt()
    receipt.failed(
        "presync_local_mcp",
        reason="uv sync failed",
        failure_class="system",
        criticality="abort",
    )
    receipt.write_abort_snapshot(target, profile="all", source_kind="git_overlay")

    on_disk = json.loads((target / ".workstate-bootstrap.json").read_text())
    assert on_disk["profile"] == "all"
    assert on_disk["surfaces"] == []
    assert any(step["status"] == "failed" for step in on_disk["install_steps"])


def test_presync_failure_writes_abort_snapshot(
    tmp_path: Path,
    fake_remote_with_lifecycle: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the presync abort handler must not die on plan attribute
    access before persisting the snapshot (plan.request.profile, not
    plan.profile)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()

    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_lifecycle
    subprocess.run(
        ["git", "init", "--initial-branch=main", "-q"],
        cwd=str(target),
        check=True,
    )

    with patch(
        "workstate_bootstrap.install._presync_local_mcp_envs",
        side_effect=subprocess.CalledProcessError(1, ["uv", "sync"]),
    ):
        from workstate_bootstrap.install_receipt import InstallExecutionError

        with pytest.raises(InstallExecutionError):
            install(
                target=target,
                remote_url=url,
                remote_ref=ref,
                profile="all",
                mcp_servers={
                    "workstate-handoff-mcp": {"command": "uvx", "args": ["x"]}
                },
                enforce_required_surfaces=False,
            )

    on_disk = json.loads((target / ".workstate-bootstrap.json").read_text())
    assert on_disk["profile"] == "all"
    presync = next(
        step
        for step in on_disk["install_steps"]
        if step["step"] == "presync_local_mcp"
    )
    assert presync["status"] == "failed"
    assert presync["criticality"] == "abort"
    assert on_disk["mcp_servers"] == ["workstate-handoff-mcp"]