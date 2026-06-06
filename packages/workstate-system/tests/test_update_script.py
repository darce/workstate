"""implementation note implementation note: ``scripts/workstate/update.sh`` behavior contracts.

The one-shot consumer updater is deterministic POSIX shell. These tests run
the real script against stubbed PATH binaries and fixture manifests:

* syntax-clean under ``sh -n`` (and shellcheck when available);
* adapter detection resolves the Python runtime owning the
  ``workstate-bootstrap`` console script (dry-run prints the plan);
* a package-source manifest drives ``workstate-bootstrap update --target``;
* a git_overlay manifest without ``REMOTE_REF`` produces guidance and exit 2
  — never a mutation;
* missing workstate-bootstrap on PATH is a clear failure.

``WORKSTATE_UPDATE_DRY_RUN=1`` prints each step instead of executing the
upgrade/update/doctor commands — that is the unit seam (and an operator
preview), not a mock.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
UPDATE_SH = (
    REPO_ROOT
    / "packages"
    / "workstate-system"
    / "workstate_system"
    / "payload"
    / "scripts"
    / "workstate"
    / "update.sh"
)
UPDATE_MK = (
    REPO_ROOT
    / "packages"
    / "workstate-system"
    / "workstate_system"
    / "payload"
    / "Makefile.d"
    / "update.mk"
)


def test_payload_files_exist() -> None:
    assert UPDATE_SH.is_file()
    assert UPDATE_MK.is_file()
    assert "workstate-update:" in UPDATE_MK.read_text(encoding="utf-8")
    assert UPDATE_SH.stat().st_mode & stat.S_IXUSR, "update.sh must be executable"


def test_update_sh_is_posix_syntax_clean() -> None:
    proc = subprocess.run(["sh", "-n", str(UPDATE_SH)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_update_sh_is_shellcheck_clean() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed")
    proc = subprocess.run(
        [shellcheck, "--shell=sh", str(UPDATE_SH)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _consumer(tmp_path: Path, manifest: dict[str, object]) -> Path:
    target = tmp_path / "consumer"
    target.mkdir()
    (target / ".workstate-bootstrap.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return target


def _stub_bootstrap(tmp_path: Path) -> Path:
    """A workstate-bootstrap console-script stub whose shebang names the real
    python (the runtime-resolution seam) and which logs its argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "bootstrap-calls.log"
    stub = bin_dir / "workstate-bootstrap"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "print('stub-bootstrap ok')\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir


def _run(
    target: Path,
    bin_dir: Path,
    *,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["WORKSTATE_UPDATE_DRY_RUN"] = "1"
    env.update(env_extra or {})
    return subprocess.run(
        ["sh", str(UPDATE_SH), str(target)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


PACKAGE_MANIFEST: dict[str, object] = {
    "schema_version": 2,
    "source_kind": "package",
    "package_version": "0.2.1",
    "stack_distribution": "workstate-stack",
    "stack_version": "0.1.0",
    "stack_members": {"workstate-system": "0.2.1"},
    "surfaces": [],
    "configs": [],
    "mcp_servers": [],
}
GIT_MANIFEST: dict[str, object] = {
    "schema_version": 2,
    "remote_url": "file:///tmp/fake.git",
    "remote_ref": "v0.1.0",
    "remote_sha": "0" * 40,
    "surfaces": [],
    "configs": [],
    "mcp_servers": [],
}


def test_dry_run_package_source_plans_upgrade_update_doctor(
    tmp_path: Path,
) -> None:
    target = _consumer(tmp_path, PACKAGE_MANIFEST)
    bin_dir = _stub_bootstrap(tmp_path)

    proc = _run(target, bin_dir)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "workstate-stack" in out  # upgrade step planned
    assert "update --target" in out
    assert "--remote-ref" not in out  # package source never passes a ref
    assert "doctor --target" in out


def test_dry_run_resolves_runtime_from_console_script_shebang(
    tmp_path: Path,
) -> None:
    target = _consumer(tmp_path, PACKAGE_MANIFEST)
    bin_dir = _stub_bootstrap(tmp_path)

    proc = _run(target, bin_dir)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sys.executable in proc.stdout, (
        "the upgrade must target the runtime that owns workstate-bootstrap"
    )


def test_git_overlay_without_remote_ref_guides_and_exits_2(
    tmp_path: Path,
) -> None:
    target = _consumer(tmp_path, GIT_MANIFEST)
    bin_dir = _stub_bootstrap(tmp_path)

    proc = _run(target, bin_dir)

    assert proc.returncode == 2
    combined = proc.stdout + proc.stderr
    assert "REMOTE_REF" in combined
    assert "install --source package" in combined  # migration guidance
    log = tmp_path / "bootstrap-calls.log"
    assert not log.exists(), "guidance path must not invoke bootstrap"


def test_git_overlay_with_remote_ref_plans_ref_update(tmp_path: Path) -> None:
    target = _consumer(tmp_path, GIT_MANIFEST)
    bin_dir = _stub_bootstrap(tmp_path)

    proc = _run(target, bin_dir, env_extra={"REMOTE_REF": "v9.9.9"})

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--remote-ref v9.9.9" in proc.stdout


def test_missing_bootstrap_is_a_clear_failure(tmp_path: Path) -> None:
    target = _consumer(tmp_path, PACKAGE_MANIFEST)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = {
        # System dirs keep sh/git resolvable; no python env dirs, so the
        # workstate-bootstrap console script itself is absent.
        "PATH": f"{empty_bin}:/usr/bin:/bin",
        "WORKSTATE_UPDATE_DRY_RUN": "1",
    }
    proc = subprocess.run(
        ["sh", str(UPDATE_SH), str(target)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode != 0
    assert "workstate-bootstrap" in (proc.stdout + proc.stderr)


def _stub_pipx_owned_bootstrap(tmp_path: Path) -> Path:
    """Console script living under a pipx venvs layout, plus a pipx stub."""
    venv_bin = tmp_path / "pipx" / "venvs" / "workstate-bootstrap" / "bin"
    venv_bin.mkdir(parents=True)
    stub = venv_bin / "workstate-bootstrap"
    stub.write_text(
        f"#!{sys.executable}\nprint('stub-bootstrap ok')\n", encoding="utf-8"
    )
    stub.chmod(0o755)
    pipx = tmp_path / "bin"
    pipx.mkdir(exist_ok=True)
    pipx_stub = pipx / "pipx"
    pipx_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pipx_stub.chmod(0o755)
    return venv_bin


def test_pipx_owned_runtime_selects_pipx_inject_adapter(tmp_path: Path) -> None:
    target = _consumer(tmp_path, PACKAGE_MANIFEST)
    venv_bin = _stub_pipx_owned_bootstrap(tmp_path)

    env = dict(os.environ)
    env["PATH"] = f"{venv_bin}:{tmp_path / 'bin'}:{env['PATH']}"
    env["WORKSTATE_UPDATE_DRY_RUN"] = "1"
    proc = subprocess.run(
        ["sh", str(UPDATE_SH), str(target)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "pipx inject" in proc.stdout
    assert "uv pip install" not in proc.stdout


def test_without_uv_falls_back_to_runtime_pip(tmp_path: Path) -> None:
    target = _consumer(tmp_path, PACKAGE_MANIFEST)
    bin_dir = _stub_bootstrap(tmp_path)

    env = {
        # bin_dir + system dirs only: no uv, no pipx anywhere on PATH.
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "WORKSTATE_UPDATE_DRY_RUN": "1",
    }
    proc = subprocess.run(
        ["sh", str(UPDATE_SH), str(target)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "-m pip install --upgrade workstate-stack" in proc.stdout
    assert "uv pip install" not in proc.stdout


def test_real_run_verifies_stack_member_pins_before_update(tmp_path: Path) -> None:
    target = _consumer(tmp_path, PACKAGE_MANIFEST)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "runtime.log"

    runtime = bin_dir / "fake-python"
    runtime.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        f"LOG={str(log)!r}\n"
        'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then\n'
        '    echo pip-install >> "$LOG"\n'
        "    exit 0\n"
        "fi\n"
        'if [ "${1:-}" = "-" ]; then\n'
        "    script=$(cat)\n"
        "    printf '%s' \"$script\" | grep 'metadata.requires' >/dev/null\n"
        '    echo verify-pins >> "$LOG"\n'
        "    echo 'workstate-update: verified fake pins'\n"
        "    exit 0\n"
        "fi\n"
        'if [ "${1:-}" = "-c" ]; then\n'
        '    if printf \'%s\' "${2:-}" | grep "source_kind" >/dev/null; then\n'
        "        echo package\n"
        "    else\n"
        "        echo 'workstate-update: stack versions (installed / manifest):'\n"
        "    fi\n"
        "    exit 0\n"
        "fi\n"
        'echo "bootstrap ${2:-}" >> "$LOG"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    runtime.chmod(0o755)

    bootstrap = bin_dir / "workstate-bootstrap"
    # macOS rejects a script as a shebang interpreter (ENOEXEC) and sh falls
    # back to interpreting this file itself — the exec line reproduces the
    # kernel's argv (interpreter, script-path, args) so both paths converge.
    bootstrap.write_text(f'#!{runtime}\nexec {runtime} "$0" "$@"\n', encoding="utf-8")
    bootstrap.chmod(0o755)

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }
    proc = subprocess.run(
        ["sh", str(UPDATE_SH), str(target)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines.index("pip-install") < lines.index("verify-pins")
    assert proc.stdout.index(
        "workstate-update: verified fake pins"
    ) < proc.stdout.index("workstate-bootstrap update --target")


def test_version_table_failure_does_not_mask_doctor_status(tmp_path: Path) -> None:
    """revD regression: the informational version table runs under ``set -e``
    just before ``exit "$DOCTOR_STATUS"`` — if its interpreter call fails the
    script must still exit with doctor's code, not the table's."""
    target = _consumer(tmp_path, PACKAGE_MANIFEST)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    runtime = bin_dir / "fake-python"
    runtime.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then\n'
        "    exit 0\n"
        "fi\n"
        'if [ "${1:-}" = "-" ]; then\n'
        "    cat >/dev/null\n"
        "    exit 0\n"
        "fi\n"
        'if [ "${1:-}" = "-c" ]; then\n'
        '    if printf \'%s\' "${2:-}" | grep "source_kind" >/dev/null; then\n'
        "        echo package\n"
        "        exit 0\n"
        "    fi\n"
        "    exit 1\n"  # the version-table call fails (e.g. unreadable manifest)
        "fi\n"
        'if [ "${2:-}" = "doctor" ]; then\n'
        "    exit 3\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    runtime.chmod(0o755)

    bootstrap = bin_dir / "workstate-bootstrap"
    # Shebang names the runtime (resolution seam); the exec line is the macOS
    # ENOEXEC sh-fallback dispatch (see the pin-verification test above).
    bootstrap.write_text(f'#!{runtime}\nexec {runtime} "$0" "$@"\n', encoding="utf-8")
    bootstrap.chmod(0o755)

    proc = subprocess.run(
        ["sh", str(UPDATE_SH), str(target)],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        timeout=60,
    )

    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "version table unavailable" in proc.stdout
