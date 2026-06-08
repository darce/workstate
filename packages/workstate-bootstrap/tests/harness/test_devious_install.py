"""implementation note S7: devious-harness install scenario matrix."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from .fakes import (
    prepend_path,
    write_garbage_generator,
    write_grok_timeout_fake,
    write_half_write_generator,
    write_hanging_git_fake,
    write_hanging_uvx_fake,
    write_offline_uv_fake,
)
from tests.test_install import fake_remote_with_generator  # noqa: F401
from tests.test_install_profiles import fake_remote_with_lifecycle  # noqa: F401
from workstate_bootstrap.external import (
    ExternalCallTimeout,
    reset_offline_latch,
    run_external,
)
from workstate_bootstrap.install_receipt import receipt_failed_steps


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=30,
    ).stdout.strip()


def test_hanging_git_fake_times_out_at_class_gateway(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_hanging_git_fake(bin_dir)
    env = prepend_path(os.environ, bin_dir)
    reset_offline_latch()
    with pytest.raises(ExternalCallTimeout) as excinfo:
        run_external(
            ["git", "fetch"],
            call_class="git",
            env=env,
            timeout_override=1,
            check=True,
        )
    assert excinfo.value.call_class == "git"


def test_half_write_generator_exits_nonzero(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workstate_bootstrap.external as external

    from workstate_bootstrap.install import install

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    target = tmp_path / "consumer"
    target.mkdir()
    _git("init", "--initial-branch=main", cwd=target)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gen = write_half_write_generator(bin_dir)
    real_run = external.run_external

    def flaky_generator(cmd, **kwargs):
        if kwargs.get("call_class") == "generator":
            cmd = [str(fake_gen), *cmd[2:]]
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(external, "run_external", flaky_generator)

    url, ref = fake_remote_with_generator
    with pytest.raises(subprocess.CalledProcessError):
        install(
            target=target,
            remote_url=url,
            remote_ref=ref,
            profile="all",
            mcp_servers=None,
            enforce_required_surfaces=False,
        )
    assert (target / ".github" / "prompts" / "partial.prompt.md").is_file()
    # Failure-mode contract: an aborted install must not finalize a success
    # manifest — the partial artifact above persists, but no
    # .workstate-bootstrap.json may claim the install completed.
    from workstate_bootstrap.install import BOOTSTRAP_MANIFEST_NAME

    assert not (target / BOOTSTRAP_MANIFEST_NAME).exists()


def test_grok_timeout_records_failed_activation(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.activation import activate_grok_plugin
    from workstate_bootstrap.install import install

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref, mcp_servers=None)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_grok_timeout_fake(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    with patch("workstate_bootstrap.external.timeout_for_call_class", return_value=1):
        entry = activate_grok_plugin(target)
    assert entry["action"] == "failed"
    assert "timed out" in entry.get("message", "")


def _presync_via_offline_uv(target: Path, mcp_servers: object) -> list[Path]:
    """Force a presync uv_sync call so the offline PATH fake aborts install."""
    from workstate_bootstrap.external import run_external

    run_external(["uv", "sync"], call_class="uv_sync", cwd=str(target))
    return []


def test_presync_abort_writes_classified_receipt_and_cli_exits_system(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import DEFAULT_MCP_SERVERS, install
    from workstate_bootstrap.install_receipt import InstallExecutionError

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    target = tmp_path / "consumer"
    target.mkdir()
    _git("init", "--initial-branch=main", cwd=target)
    _git("config", "user.email", "devious@example.com", cwd=target)
    _git("config", "user.name", "Devious", cwd=target)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_offline_uv_fake(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    url, ref = fake_remote_with_generator
    with patch(
        "workstate_bootstrap.install._presync_local_mcp_envs",
        side_effect=_presync_via_offline_uv,
    ):
        with pytest.raises(InstallExecutionError) as excinfo:
            install(
                target=target,
                remote_url=url,
                remote_ref=ref,
                profile="all",
                mcp_servers=DEFAULT_MCP_SERVERS,
                enforce_required_surfaces=False,
            )
    assert excinfo.value.failure_class == "system"
    manifest = json.loads((target / ".workstate-bootstrap.json").read_text())
    presync = next(
        s for s in manifest["install_steps"] if s["step"] == "presync_local_mcp"
    )
    assert presync["status"] == "failed"
    assert presync["failure_class"] == "system"

    cli_target = tmp_path / "cli-consumer"
    cli_target.mkdir()
    _git("init", "--initial-branch=main", cwd=cli_target)
    project = cli_target / "handoff-proj"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "handoff-proj"\nversion = "0.0.0"\n'
    )
    mcp_path = tmp_path / "local-mcp.json"
    mcp_path.write_text(
        json.dumps(
            {
                "workstate-handoff-mcp": {
                    "command": "uv",
                    "args": [
                        "run",
                        "--no-sync",
                        "--project",
                        "handoff-proj",
                        "mcp-workstate-handoff",
                        "serve-stdio",
                    ],
                }
            }
        )
        + "\n"
    )
    cli_env = prepend_path(os.environ, bin_dir)
    cli_env["HOME"] = str(tmp_path / "home")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "workstate_bootstrap.cli",
            "install",
            "--target",
            str(cli_target),
            "--remote-url",
            url,
            "--remote-ref",
            ref,
            "--profile",
            "all",
            "--mcp-servers",
            str(mcp_path),
            "--no-enforce-required-surfaces",
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
        env=cli_env,
    )
    assert proc.returncode == 1


def _abort_install_via_offline_presync(
    tmp_path: Path,
    fake_remote: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    """Shared setup: drive an install to a presync abort snapshot."""
    from workstate_bootstrap.install import DEFAULT_MCP_SERVERS, install
    from workstate_bootstrap.install_receipt import InstallExecutionError

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    target = tmp_path / "consumer"
    target.mkdir()
    _git("init", "--initial-branch=main", cwd=target)
    _git("config", "user.email", "devious@example.com", cwd=target)
    _git("config", "user.name", "Devious", cwd=target)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_offline_uv_fake(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    url, ref = fake_remote
    with patch(
        "workstate_bootstrap.install._presync_local_mcp_envs",
        side_effect=_presync_via_offline_uv,
    ):
        with pytest.raises(InstallExecutionError):
            install(
                target=target,
                remote_url=url,
                remote_ref=ref,
                profile="all",
                mcp_servers=DEFAULT_MCP_SERVERS,
                enforce_required_surfaces=False,
            )
    manifest = json.loads((target / ".workstate-bootstrap.json").read_text())
    presync = next(
        s for s in manifest["install_steps"] if s["step"] == "presync_local_mcp"
    )
    assert presync["status"] == "failed"
    assert manifest.get("surfaces") in ([], None)
    return target, bin_dir


def test_repair_converges_presync_abort_without_explicit_mcp_servers(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Presync abort snapshots persist managed server names; repair must
    reconstruct the install-time MCP map without an explicit ``mcp_servers``
    argument (no false convergence via ``mcp_servers=None`` install)."""
    from workstate_bootstrap.external import reset_offline_latch
    from workstate_bootstrap.install import DEFAULT_MCP_SERVERS
    from workstate_bootstrap.subcommands import repair

    target, _bin_dir = _abort_install_via_offline_presync(
        tmp_path, fake_remote_with_generator, monkeypatch
    )
    manifest = json.loads((target / ".workstate-bootstrap.json").read_text())
    assert manifest.get("mcp_servers") == sorted(DEFAULT_MCP_SERVERS)

    reset_offline_latch()
    with (
        patch("workstate_bootstrap.install._presync_local_mcp_envs", return_value=[]),
        patch("workstate_bootstrap.install._run_init_state"),
    ):
        report = repair(target=target, mcp_servers=None)

    assert any(f.get("path") == "presync_local_mcp" for f in report["repaired"]), report
    manifest = json.loads((target / ".workstate-bootstrap.json").read_text())
    presync = next(
        s for s in manifest["install_steps"] if s["step"] == "presync_local_mcp"
    )
    assert presync["status"] == "ok"
    assert manifest["surfaces"], "re-run install must materialize surfaces"


def test_repair_converges_failed_presync_abort_via_install_rerun(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """implementation note implementation note (S6-02): a presync abort leaves an INCOMPLETE
    install (no surfaces/configs), so repair must converge by re-running the
    full install from the abort snapshot's recorded inputs — not by flipping
    the receipt step in place."""
    from workstate_bootstrap.external import reset_offline_latch
    from workstate_bootstrap.install import DEFAULT_MCP_SERVERS
    from workstate_bootstrap.subcommands import repair

    target, _bin_dir = _abort_install_via_offline_presync(
        tmp_path, fake_remote_with_generator, monkeypatch
    )

    # Connectivity restored: presync now succeeds.
    reset_offline_latch()
    with (
        patch("workstate_bootstrap.install._presync_local_mcp_envs", return_value=[]),
        patch("workstate_bootstrap.install._run_init_state"),
    ):
        report = repair(target=target, mcp_servers=DEFAULT_MCP_SERVERS)

    assert any(f.get("path") == "presync_local_mcp" for f in report["repaired"]), report
    manifest = json.loads((target / ".workstate-bootstrap.json").read_text())
    presync = next(
        s for s in manifest["install_steps"] if s["step"] == "presync_local_mcp"
    )
    assert presync["status"] == "ok"
    # The convergence is a real install, not a receipt flip: surfaces exist.
    assert manifest["surfaces"], "re-run install must materialize surfaces"
    assert (target / ".workstate" / "remote").exists()


def test_repair_leaves_presync_abort_snapshot_when_still_offline(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.external import reset_offline_latch
    from workstate_bootstrap.install import DEFAULT_MCP_SERVERS
    from workstate_bootstrap.subcommands import repair

    target, _bin_dir = _abort_install_via_offline_presync(
        tmp_path, fake_remote_with_generator, monkeypatch
    )
    before = (target / ".workstate-bootstrap.json").read_text()

    # Still offline: the re-run aborts again; snapshot must stay intact.
    reset_offline_latch()
    with patch(
        "workstate_bootstrap.install._presync_local_mcp_envs",
        side_effect=_presync_via_offline_uv,
    ):
        report = repair(target=target, mcp_servers=DEFAULT_MCP_SERVERS)

    assert not any(f.get("path") == "presync_local_mcp" for f in report["repaired"])
    assert any(
        f.get("kind") == "install_step_repair_skipped"
        and f.get("path") == "presync_local_mcp"
        for f in report["skipped"]
    ), report
    after = json.loads((target / ".workstate-bootstrap.json").read_text())
    presync = next(
        s for s in after["install_steps"] if s["step"] == "presync_local_mcp"
    )
    assert presync["status"] == "failed"
    assert json.loads(before)["install_steps"] == after["install_steps"]


def test_one_failed_config_writer_bulkheads_and_doctor_surfaces_it(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """implementation note implementation note (S6-01): per-surface config receipts. One failing
    harness config writer must not abort the remaining surfaces; the failure
    lands as a failed config_<harness> StepReceipt in install_steps so the
    existing doctor receipt path surfaces it."""
    from workstate_bootstrap.install import DEFAULT_MCP_SERVERS, install
    from workstate_bootstrap.subcommands import doctor

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    target = tmp_path / "consumer"
    target.mkdir()
    _git("init", "--initial-branch=main", cwd=target)
    _git("config", "user.email", "devious@example.com", cwd=target)
    _git("config", "user.name", "Devious", cwd=target)

    url, ref = fake_remote_with_generator
    with (
        patch("workstate_bootstrap.install._presync_local_mcp_envs", return_value=[]),
        patch("workstate_bootstrap.install._prewarm_uvx_mcp_envs", return_value=[]),
        patch("workstate_bootstrap.install._run_init_state"),
        patch(
            "workstate_bootstrap.install._write_vscode_mcp_json",
            side_effect=OSError("disk full while writing .vscode/mcp.json"),
        ),
    ):
        manifest = install(
            target=target,
            remote_url=url,
            remote_ref=ref,
            profile="all",
            mcp_servers=DEFAULT_MCP_SERVERS,
            enforce_required_surfaces=False,
        )

    # Bulkhead: the other two MCP config surfaces were still written.
    config_paths = {entry["path"] for entry in manifest["configs"]}
    assert ".mcp.json" in config_paths
    assert ".codex/config.toml" in config_paths
    assert ".vscode/mcp.json" not in config_paths
    assert (target / ".mcp.json").is_file()
    assert (target / ".codex" / "config.toml").is_file()

    # Receipt: per-surface steps, mixed ok/failed.
    steps = {
        s["step"]: s
        for s in manifest["install_steps"]
        if str(s.get("step", "")).startswith("config_")
    }
    assert steps["config_claude"]["status"] == "ok"
    assert steps["config_codex"]["status"] == "ok"
    assert steps["config_vscode"]["status"] == "failed"
    assert steps["config_vscode"]["failure_class"] == "system"

    # Doctor surfaces the failed step via the existing install_steps path.
    findings = doctor(target=target, mcp_servers=DEFAULT_MCP_SERVERS)
    assert any(
        f["kind"] == "install_step_receipt" and f["path"] == "config_vscode"
        for f in findings
    ), findings


def test_one_failed_config_writer_bulkheads_value_error_as_application(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import DEFAULT_MCP_SERVERS, install

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    target = tmp_path / "consumer"
    target.mkdir()
    _git("init", "--initial-branch=main", cwd=target)
    _git("config", "user.email", "devious@example.com", cwd=target)
    _git("config", "user.name", "Devious", cwd=target)

    url, ref = fake_remote_with_generator
    with (
        patch("workstate_bootstrap.install._presync_local_mcp_envs", return_value=[]),
        patch("workstate_bootstrap.install._prewarm_uvx_mcp_envs", return_value=[]),
        patch("workstate_bootstrap.install._run_init_state"),
        patch(
            "workstate_bootstrap.install._write_codex_config",
            side_effect=ValueError("invalid codex MCP table"),
        ),
    ):
        manifest = install(
            target=target,
            remote_url=url,
            remote_ref=ref,
            profile="all",
            mcp_servers=DEFAULT_MCP_SERVERS,
            enforce_required_surfaces=False,
        )

    steps = {
        s["step"]: s
        for s in manifest["install_steps"]
        if str(s.get("step", "")).startswith("config_")
    }
    assert steps["config_claude"]["status"] == "ok"
    assert steps["config_vscode"]["status"] == "ok"
    assert steps["config_codex"]["status"] == "failed"
    assert steps["config_codex"]["failure_class"] == "application"

    from workstate_bootstrap.subcommands import doctor

    findings = doctor(target=target, mcp_servers=DEFAULT_MCP_SERVERS)
    assert any(
        f["kind"] == "install_step_receipt" and f["path"] == "config_codex"
        for f in findings
    ), findings


def test_cli_install_exits_2_on_application_preflight_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workstate_bootstrap import cli
    from workstate_bootstrap.install_plan import SURFACE_MODE_COPY, SourceResolver

    target = tmp_path / "consumer"
    target.mkdir()
    missing = tmp_path / "missing-payload"

    def fake_resolve(_package_root: Path | None) -> SourceResolver:
        return SourceResolver(
            root=missing,
            kind="package",
            base_anchor="0" * 40,
            surface_mode=SURFACE_MODE_COPY,
        )

    monkeypatch.setattr(
        "workstate_bootstrap.install_plan.resolve_package_source",
        fake_resolve,
    )
    code = cli.main(
        [
            "install",
            "--target",
            str(target),
            "--source",
            "package",
            "--no-mcp-servers",
        ]
    )
    assert code == 2


def test_offline_prewarm_defers_and_install_completes_with_manifest(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import DEFAULT_MCP_SERVERS, install

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    target = tmp_path / "consumer"
    target.mkdir()
    _git("init", "--initial-branch=main", cwd=target)
    _git("config", "user.email", "devious@example.com", cwd=target)
    _git("config", "user.name", "Devious", cwd=target)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_offline_uv_fake(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    with (
        patch("workstate_bootstrap.install._presync_local_mcp_envs", return_value=[]),
        patch("workstate_bootstrap.install._run_init_state"),
    ):
        url, ref = fake_remote_with_generator
        manifest = install(
            target=target,
            remote_url=url,
            remote_ref=ref,
            profile="all",
            mcp_servers=DEFAULT_MCP_SERVERS,
            enforce_required_surfaces=False,
        )

    prewarm = next(
        s for s in manifest["install_steps"] if s["step"] == "prewarm_uvx_mcp"
    )
    assert prewarm["status"] == "deferred"
    assert manifest.get("offline_latch") is True


def test_hanging_uvx_prewarm_defers_and_install_completes(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First-call prewarm timeout must defer, not abort the install.

    A hanging uvx raises ExternalCallTimeout on the FIRST prewarm call (the
    offline latch trips only for subsequent calls), so the step handler must
    catch it — otherwise the install aborts with no classified receipt.
    """
    from workstate_bootstrap.install import DEFAULT_MCP_SERVERS, install

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    target = tmp_path / "consumer"
    target.mkdir()
    _git("init", "--initial-branch=main", cwd=target)
    _git("config", "user.email", "devious@example.com", cwd=target)
    _git("config", "user.name", "Devious", cwd=target)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_hanging_uvx_fake(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("WORKSTATE_TIMEOUT_UVX_PREWARM", "1")
    reset_offline_latch()

    with (
        patch("workstate_bootstrap.install._presync_local_mcp_envs", return_value=[]),
        patch("workstate_bootstrap.install._run_init_state"),
    ):
        url, ref = fake_remote_with_generator
        manifest = install(
            target=target,
            remote_url=url,
            remote_ref=ref,
            profile="all",
            mcp_servers=DEFAULT_MCP_SERVERS,
            enforce_required_surfaces=False,
        )

    prewarm = next(
        s for s in manifest["install_steps"] if s["step"] == "prewarm_uvx_mcp"
    )
    assert prewarm["status"] == "deferred"
    assert manifest.get("offline_latch") is True


def test_garbage_generator_exits_nonzero(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workstate_bootstrap.external as external

    from workstate_bootstrap.install import install

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    target = tmp_path / "consumer"
    target.mkdir()
    _git("init", "--initial-branch=main", cwd=target)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gen = write_garbage_generator(bin_dir)
    real_run = external.run_external

    def garbage_generator(cmd, **kwargs):
        if kwargs.get("call_class") == "generator":
            cmd = [str(fake_gen), *cmd[2:]]
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(external, "run_external", garbage_generator)

    url, ref = fake_remote_with_generator
    with pytest.raises(subprocess.CalledProcessError):
        install(
            target=target,
            remote_url=url,
            remote_ref=ref,
            profile="all",
            mcp_servers=None,
            enforce_required_surfaces=False,
        )


def test_repair_converges_deferred_prewarm_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.subcommands import repair

    target = tmp_path / "consumer"
    target.mkdir()
    manifest = {
        "schema_version": 2,
        "profile": "all",
        "surfaces": [],
        "configs": [{"path": ".mcp.json", "action": "created"}],
        "mcp_servers": ["workstate-handoff-mcp"],
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
    (target / ".workstate-bootstrap.json").write_text(json.dumps(manifest) + "\n")
    (target / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"workstate-handoff-mcp": {"command": "uvx", "args": ["x"]}}}
        )
    )

    with patch(
        "workstate_bootstrap.install._prewarm_uvx_mcp_envs",
        return_value=["mcp-workstate-handoff@0.1.0"],
    ):
        report = repair(target=target, mcp_servers=None)

    assert report["repaired"]
    on_disk = json.loads((target / ".workstate-bootstrap.json").read_text())
    prewarm = next(
        s for s in on_disk["install_steps"] if s["step"] == "prewarm_uvx_mcp"
    )
    assert prewarm["status"] == "ok"


def test_doctor_surfaces_receipt_failures(tmp_path: Path) -> None:
    from workstate_bootstrap.subcommands import doctor

    target = tmp_path / "consumer"
    target.mkdir()
    manifest = {
        "schema_version": 2,
        "profile": "all",
        "surfaces": [],
        "configs": [],
        "mcp_servers": [],
        "install_steps": [
            {
                "step": "prewarm_uvx_mcp",
                "status": "deferred",
                "reason": "offline",
                "failure_class": "system",
            }
        ],
    }
    (target / ".workstate-bootstrap.json").write_text(json.dumps(manifest) + "\n")
    (target / ".workstate").mkdir()
    (target / ".workstate" / "remote").mkdir()

    findings = doctor(target=target)
    assert any(f["kind"] == "install_step_receipt" for f in findings)


def test_doctor_reports_corrupt_manifest_instead_of_crashing(tmp_path: Path) -> None:
    """Half-written manifest JSON is a finding, not a doctor crash."""
    from workstate_bootstrap.subcommands import doctor

    target = tmp_path / "consumer"
    target.mkdir()
    (target / ".workstate-bootstrap.json").write_text('{"schema_version": 2, "surf')
    (target / ".workstate").mkdir()
    (target / ".workstate" / "remote").mkdir()

    findings = doctor(target=target)
    assert any(f["kind"] == "corrupt_manifest" for f in findings)


def test_status_rejects_corrupt_manifest_with_actionable_error(tmp_path: Path) -> None:
    from workstate_bootstrap.subcommands import status

    target = tmp_path / "consumer"
    target.mkdir()
    (target / ".workstate-bootstrap.json").write_text('{"schema_version": 2, "surf')

    with pytest.raises(ValueError, match="not valid JSON"):
        status(target=target)


def test_receipt_failed_steps_helper() -> None:
    manifest = {
        "install_steps": [
            {"step": "ok_step", "status": "ok"},
            {"step": "bad", "status": "failed", "reason": "boom"},
        ]
    }
    failed = receipt_failed_steps(manifest)
    assert len(failed) == 1
    assert failed[0]["step"] == "bad"
