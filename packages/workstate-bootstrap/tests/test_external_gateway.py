"""implementation note S5: timed-subprocess gateway tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from workstate_bootstrap.external import (
    DEFAULT_TIMEOUTS,
    DeferredExternalCall,
    ExternalCallTimeout,
    offline_latch_active,
    reset_offline_latch,
    run_external,
    timeout_for_call_class,
)


def test_timeout_defaults_match_legacy_literals() -> None:
    assert DEFAULT_TIMEOUTS["git"] == 120
    assert DEFAULT_TIMEOUTS["generator"] == 120
    assert DEFAULT_TIMEOUTS["uv_sync"] == 300


def test_workstate_timeout_git_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKSTATE_TIMEOUT_GIT", "7")
    assert timeout_for_call_class("git") == 7


def test_hanging_git_fails_at_class_timeout(tmp_path: Path) -> None:
    hang = tmp_path / "hang.sh"
    hang.write_text("#!/bin/sh\nsleep 30\n")
    hang.chmod(0o755)
    reset_offline_latch()
    with pytest.raises(ExternalCallTimeout) as excinfo:
        run_external(
            [str(hang)],
            call_class="git",
            timeout_override=1,
        )
    assert excinfo.value.call_class == "git"
    assert excinfo.value.timeout == 1


def test_offline_latch_skips_remaining_best_effort_calls() -> None:
    reset_offline_latch()
    with pytest.raises(subprocess.CalledProcessError):
        run_external(["sh", "-c", "exit 1"], call_class="uv_sync", check=True)
    assert offline_latch_active()
    with pytest.raises(DeferredExternalCall) as excinfo:
        run_external(["uvx", "python", "-c", "1"], call_class="uvx_prewarm", check=False)
    assert excinfo.value.call_class == "uvx_prewarm"
    reset_offline_latch()


def test_timeout_trips_offline_latch_for_best_effort_class(tmp_path: Path) -> None:
    hang = tmp_path / "hang.sh"
    hang.write_text("#!/bin/sh\nsleep 30\n")
    hang.chmod(0o755)
    reset_offline_latch()
    with pytest.raises(ExternalCallTimeout):
        run_external([str(hang)], call_class="uv_sync", timeout_override=1)
    assert offline_latch_active()
    reset_offline_latch()


def test_timeout_does_not_trip_latch_for_required_class(tmp_path: Path) -> None:
    hang = tmp_path / "hang.sh"
    hang.write_text("#!/bin/sh\nsleep 30\n")
    hang.chmod(0o755)
    reset_offline_latch()
    with pytest.raises(ExternalCallTimeout):
        run_external([str(hang)], call_class="git", timeout_override=1)
    assert not offline_latch_active()


def test_malformed_timeout_env_override_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKSTATE_TIMEOUT_GIT", "not-a-number")
    with pytest.raises(ValueError, match="WORKSTATE_TIMEOUT_GIT"):
        timeout_for_call_class("git")


def test_timeout_reaps_grandchildren(tmp_path: Path) -> None:
    """A timed-out wrapper's backgrounded grandchild must not survive the kill."""
    import os
    import time

    pidfile = tmp_path / "grandchild.pid"
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        "sleep 30 &\n"
        f"echo $! > {pidfile}\n"
        "wait\n"
    )
    wrapper.chmod(0o755)
    reset_offline_latch()
    with pytest.raises(ExternalCallTimeout):
        run_external([str(wrapper)], call_class="git", timeout_override=1)
    assert pidfile.is_file(), "wrapper never started"
    grandchild_pid = int(pidfile.read_text().strip())
    for _ in range(20):
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(grandchild_pid, 9)
        pytest.fail(f"grandchild {grandchild_pid} survived the timeout kill")


def test_timeout_reap_bounded_when_killpg_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reap must not wedge when killpg fails and a grandchild holds the pipes.

    Forces the ``_kill_process_tree`` killpg fallback (plain ``kill()`` reaps
    only the direct child); the backgrounded grandchild inherits the captured
    stdout pipe and keeps it open, so an untimed post-kill ``communicate()``
    would block until the grandchild exits — defeating the class timeout.
    """
    import os
    import time

    import workstate_bootstrap.external as external

    monkeypatch.setattr(
        external.os, "killpg", lambda *a: (_ for _ in ()).throw(PermissionError())
    )
    pidfile = tmp_path / "grandchild.pid"
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        "sleep 30 &\n"
        f"echo $! > {pidfile}\n"
        "wait\n"
    )
    wrapper.chmod(0o755)
    reset_offline_latch()
    start = time.monotonic()
    try:
        with pytest.raises(ExternalCallTimeout):
            run_external(
                [str(wrapper)],
                call_class="git",
                timeout_override=1,
                capture_output=True,
            )
        elapsed = time.monotonic() - start
        # 1s class timeout + bounded reap (5s) + slack; the unbounded bug
        # blocked ~30s until the grandchild's sleep finished.
        assert elapsed < 15, f"timeout reap took {elapsed:.1f}s; reap is unbounded"
    finally:
        if pidfile.is_file():
            try:
                os.kill(int(pidfile.read_text().strip()), 9)
            except (ProcessLookupError, ValueError):
                pass


def test_bootstrap_package_routes_subprocess_via_gateway() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "workstate_bootstrap"
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        if path.name == "external.py":
            continue
        if "subprocess.run(" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == [], f"direct subprocess.run remains in: {offenders}"