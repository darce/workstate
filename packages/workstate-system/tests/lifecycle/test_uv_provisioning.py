"""WORKSTATE-REF-41 implementation note: ``uv`` preflight + ``uv sync --extra dev`` helpers.

Covers the standalone helpers in
``packages/workstate-system/scripts/workstate/lifecycle/uv_provisioning.py``.
Integration tests for the lifecycle handlers that call these helpers
live alongside the existing ``test_task_start.py`` /
``test_slice_start.py`` files.

The tests use a fake ``uv`` binary on the override env var
(``WORKSTATE_LIFECYCLE_UV_BIN``) so behavior is deterministic without
PATH manipulation.
"""

from __future__ import annotations

import io
import os
import stat
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PACKAGE_ROOT / "scripts"
LIFECYCLE_PKG_DIR = SCRIPTS_DIR / "workstate" / "lifecycle"
if str(LIFECYCLE_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(LIFECYCLE_PKG_DIR))

import uv_provisioning  # noqa: WORKSTATE-REF-402


def _write_script(target: Path, body: str) -> None:
    target.write_text(body)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _fake_uv(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "fake-uv"
    _write_script(target, body)
    return target


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(uv_provisioning.UV_BIN_ENV, raising=False)
    monkeypatch.delenv(uv_provisioning.SYNC_PACKAGES_ENV, raising=False)


def test_uv_preflight_success_when_binary_reports_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_uv(tmp_path, "#!/usr/bin/env bash\necho 'uv 0.4.0'\nexit 0\n")
    monkeypatch.setenv(uv_provisioning.UV_BIN_ENV, str(fake))

    result = uv_provisioning.uv_preflight()
    assert result.ok is True
    assert "uv 0.4.0" in result.version_output
    assert result.error == ""


def test_uv_preflight_failure_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(uv_provisioning.UV_BIN_ENV, "/nonexistent/no-such-uv-xyz")
    result = uv_provisioning.uv_preflight()
    assert result.ok is False
    assert "not found on PATH" in result.error


def test_uv_preflight_failure_when_binary_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_uv(tmp_path, "#!/usr/bin/env bash\necho boom 1>&2\nexit 5\n")
    monkeypatch.setenv(uv_provisioning.UV_BIN_ENV, str(fake))

    result = uv_provisioning.uv_preflight()
    assert result.ok is False
    assert "exited 5" in result.error
    assert "boom" in result.error


def _seed_pkg(root: Path, name: str) -> Path:
    pkg = root / "packages" / name
    pkg.mkdir(parents=True)
    (pkg / "pyproject.toml").write_text("[project]\nname='x'\n")
    return pkg


def test_discover_packages_returns_only_dirs_with_pyproject(tmp_path: Path) -> None:
    _seed_pkg(tmp_path, "alpha")
    _seed_pkg(tmp_path, "bravo")
    (tmp_path / "packages" / "no-pyproject").mkdir()

    discovered = uv_provisioning.discover_packages(tmp_path)
    names = sorted(p.name for p in discovered)
    assert names == ["alpha", "bravo"]


def test_discover_packages_honors_override(tmp_path: Path) -> None:
    _seed_pkg(tmp_path, "alpha")
    _seed_pkg(tmp_path, "bravo")
    _seed_pkg(tmp_path, "charlie")

    discovered = uv_provisioning.discover_packages(tmp_path, override="bravo,alpha")
    assert [p.name for p in discovered] == ["bravo", "alpha"]


def test_discover_packages_returns_empty_when_packages_dir_missing(tmp_path: Path) -> None:
    assert uv_provisioning.discover_packages(tmp_path) == []


def test_uv_sync_packages_runs_uv_in_each_discovered_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_pkg(tmp_path, "alpha")
    _seed_pkg(tmp_path, "bravo")
    log = tmp_path / "uv-log.txt"
    fake = _fake_uv(
        tmp_path,
        f"#!/usr/bin/env bash\necho \"called=$* cwd=$(pwd)\" >> {log}\nexit 0\n",
    )
    monkeypatch.setenv(uv_provisioning.UV_BIN_ENV, str(fake))

    stream = io.StringIO()
    ok, results = uv_provisioning.uv_sync_packages(tmp_path, stream=stream)

    assert ok is True
    assert [r.package for r in results] == ["alpha", "bravo"]
    assert all(r.ok for r in results)
    log_text = log.read_text()
    assert log_text.count("called=sync --extra dev") == 2
    assert "cwd=" + str((tmp_path / "packages" / "alpha").resolve()) in log_text
    assert "cwd=" + str((tmp_path / "packages" / "bravo").resolve()) in log_text
    assert "uv sync: alpha" in stream.getvalue()
    assert "uv sync: bravo" in stream.getvalue()


def test_uv_sync_packages_stops_at_first_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_pkg(tmp_path, "alpha")
    _seed_pkg(tmp_path, "bravo")
    fake = _fake_uv(
        tmp_path,
        '#!/usr/bin/env bash\nif [[ "$PWD" == *bravo ]]; then echo "bravo broken" 1>&2; exit 1; fi\nexit 0\n',
    )
    monkeypatch.setenv(uv_provisioning.UV_BIN_ENV, str(fake))

    stream = io.StringIO()
    ok, results = uv_provisioning.uv_sync_packages(tmp_path, stream=stream)

    assert ok is False
    assert [r.package for r in results] == ["alpha", "bravo"]
    assert results[0].ok is True
    assert results[1].ok is False
    assert "bravo broken" in results[1].stderr


def test_uv_sync_packages_handles_missing_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_pkg(tmp_path, "alpha")
    monkeypatch.setenv(uv_provisioning.UV_BIN_ENV, "/nonexistent/nope-xyz")

    stream = io.StringIO()
    ok, results = uv_provisioning.uv_sync_packages(tmp_path, stream=stream)
    assert ok is False
    assert results[0].returncode == 127


def test_uv_sync_packages_with_no_targets_is_noop_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(uv_provisioning.UV_BIN_ENV, "/nonexistent/should-not-be-called")
    stream = io.StringIO()
    ok, results = uv_provisioning.uv_sync_packages(tmp_path, stream=stream)
    assert ok is True
    assert results == []
    assert stream.getvalue() == ""


def test_sync_packages_override_returns_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(uv_provisioning.SYNC_PACKAGES_ENV, "alpha,bravo")
    assert uv_provisioning.sync_packages_override() == "alpha,bravo"


def test_sync_packages_override_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(uv_provisioning.SYNC_PACKAGES_ENV, raising=False)
    assert uv_provisioning.sync_packages_override() is None


# ---------------------------------------------------------------------------
# WORKSTATE-REF-07 implementation note: root ``.venv`` provisioning helper
# ---------------------------------------------------------------------------


def _seed_pkg_with_dev(root: Path, name: str) -> Path:
    pkg = root / "packages" / name
    pkg.mkdir(parents=True)
    (pkg / "pyproject.toml").write_text(
        "[project]\n"
        f"name = '{name}'\n"
        "version = '0.0.0'\n"
        "[project.optional-dependencies]\n"
        "dev = ['pytest']\n"
    )
    return pkg


def _fake_uv_root(tmp_path: Path, log: Path, *, fail_on: str = "") -> Path:
    """Fake ``uv`` that logs every invocation and materializes the venv python.

    ``fail_on`` is matched against the full argv string; when it is a
    non-empty substring of the call, that invocation exits non-zero so a
    test can target a specific command (``venv``, ``pytest``, a package
    path) for failure.
    """
    fail_clause = ""
    if fail_on:
        fail_clause = (
            f'if [[ "$*" == *"{fail_on}"* ]]; then '
            f'echo "fail {fail_on}" 1>&2; exit 1; fi\n'
        )
    body = (
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> {log}\n'
        f"{fail_clause}"
        'if [[ "$1" == "venv" ]]; then\n'
        '  mkdir -p "$2/bin"\n'
        '  : > "$2/bin/python"\n'
        '  chmod +x "$2/bin/python"\n'
        "fi\n"
        "exit 0\n"
    )
    return _fake_uv(tmp_path, body)


def _log_lines(log: Path) -> list[str]:
    return [line for line in log.read_text().splitlines() if line.strip()]


def test_provision_root_venv_command_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_pkg_with_dev(tmp_path, "alpha")
    _seed_pkg(tmp_path, "bravo")  # no dev extra
    log = tmp_path / "uv-log.txt"
    monkeypatch.setenv(uv_provisioning.UV_BIN_ENV, str(_fake_uv_root(tmp_path, log)))

    result = uv_provisioning.provision_root_venv(tmp_path, stream=io.StringIO())

    assert result.ok is True
    assert result.created is True
    venv_dir = tmp_path / ".venv"
    python_path = venv_dir / "bin" / "python"
    assert result.venv_dir == venv_dir
    assert result.python_path == python_path
    assert result.pytest_path == venv_dir / "bin" / "pytest"

    lines = _log_lines(log)
    # 1) create venv, 2) install pytest, 3+) editable installs
    assert lines[0] == f"venv {venv_dir} --seed"
    assert lines[1] == f"pip install --python {python_path} pytest"
    assert f"pip install --python {python_path} -e {tmp_path / 'packages' / 'alpha'}[dev]" in lines
    assert f"pip install --python {python_path} -e {tmp_path / 'packages' / 'bravo'}" in lines

    installed = {i.package: i for i in result.installs}
    assert installed["alpha"].installed is True
    assert installed["bravo"].installed is True


def test_provision_root_venv_clear_appends_clear_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKSTATE-REF-07-followups review fix: ``clear=True`` re-provisions in place.

    The manual ``provision-env`` recovery path opts in so ``uv venv`` is run
    with ``--clear`` and replaces a pre-existing ``.venv`` instead of aborting
    with "A virtual environment already exists". Default (clear=False) callers
    must not get the flag (pinned by test_provision_root_venv_command_ordering).
    """
    _seed_pkg_with_dev(tmp_path, "alpha")
    # Simulate a pre-existing (partial) .venv that bare `uv venv` would reject.
    (tmp_path / ".venv").mkdir()
    log = tmp_path / "uv-log.txt"
    monkeypatch.setenv(uv_provisioning.UV_BIN_ENV, str(_fake_uv_root(tmp_path, log)))

    result = uv_provisioning.provision_root_venv(
        tmp_path, clear=True, stream=io.StringIO()
    )

    assert result.ok is True
    assert result.created is True
    venv_dir = tmp_path / ".venv"
    assert _log_lines(log)[0] == f"venv {venv_dir} --seed --clear"


def test_provision_root_venv_no_packages_is_noop_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "uv-log.txt"
    monkeypatch.setenv(uv_provisioning.UV_BIN_ENV, str(_fake_uv_root(tmp_path, log)))

    result = uv_provisioning.provision_root_venv(tmp_path, stream=io.StringIO())

    assert result.ok is True
    assert result.created is False
    assert result.installs == []
    assert not log.exists() or _log_lines(log) == []
    assert not (tmp_path / ".venv").exists()


def test_provision_root_venv_honors_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_pkg(tmp_path, "alpha")
    _seed_pkg(tmp_path, "bravo")
    _seed_pkg(tmp_path, "charlie")
    log = tmp_path / "uv-log.txt"
    monkeypatch.setenv(uv_provisioning.UV_BIN_ENV, str(_fake_uv_root(tmp_path, log)))

    result = uv_provisioning.provision_root_venv(
        tmp_path, override="bravo", stream=io.StringIO()
    )

    assert result.ok is True
    assert [i.package for i in result.installs] == ["bravo"]


def test_provision_root_venv_venv_creation_failure_is_hard_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_pkg(tmp_path, "alpha")
    log = tmp_path / "uv-log.txt"
    monkeypatch.setenv(
        uv_provisioning.UV_BIN_ENV, str(_fake_uv_root(tmp_path, log, fail_on="venv"))
    )

    result = uv_provisioning.provision_root_venv(tmp_path, stream=io.StringIO())

    assert result.ok is False
    assert result.failure_reason
    # nothing past the failed venv create should have run
    assert all(not line.startswith("pip install") for line in _log_lines(log))


def test_provision_root_venv_pytest_install_failure_is_hard_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_pkg(tmp_path, "alpha")
    log = tmp_path / "uv-log.txt"
    monkeypatch.setenv(
        uv_provisioning.UV_BIN_ENV, str(_fake_uv_root(tmp_path, log, fail_on="pytest"))
    )

    result = uv_provisioning.provision_root_venv(tmp_path, stream=io.StringIO())

    assert result.ok is False
    assert result.failure_reason
    # editable installs must not run after a hard pytest-install failure
    assert all(" -e " not in line for line in _log_lines(log))


def test_provision_root_venv_package_conflict_is_skipped_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_pkg(tmp_path, "alpha")
    _seed_pkg(tmp_path, "bravo")
    log = tmp_path / "uv-log.txt"
    # fail only the editable install of the bravo package path
    monkeypatch.setenv(
        uv_provisioning.UV_BIN_ENV,
        str(_fake_uv_root(tmp_path, log, fail_on="packages/bravo")),
    )

    stream = io.StringIO()
    result = uv_provisioning.provision_root_venv(tmp_path, stream=stream)

    assert result.ok is True  # per-package conflict is best-effort, not fatal
    installed = {i.package: i for i in result.installs}
    assert installed["alpha"].installed is True
    assert installed["bravo"].installed is False
    assert installed["bravo"].skipped is True
    assert installed["bravo"].reason
    assert "bravo" in stream.getvalue()


def test_declares_dev_extra_detects_dev_optional_dependency(tmp_path: Path) -> None:
    with_dev = _seed_pkg_with_dev(tmp_path, "alpha")
    without_dev = _seed_pkg(tmp_path, "bravo")
    assert uv_provisioning.declares_dev_extra(with_dev) is True
    assert uv_provisioning.declares_dev_extra(without_dev) is False


def test_root_venv_env_prepends_venv_bin_when_present(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    base = {"PATH": "/usr/bin:/bin"}

    env = uv_provisioning.root_venv_env(tmp_path, base_env=base)

    assert env["PATH"].split(os.pathsep)[0] == str(venv_bin)
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")


def test_root_venv_env_is_noop_when_venv_absent(tmp_path: Path) -> None:
    base = {"PATH": "/usr/bin:/bin"}
    env = uv_provisioning.root_venv_env(tmp_path, base_env=base)
    assert env["PATH"] == "/usr/bin:/bin"
    assert "VIRTUAL_ENV" not in env
