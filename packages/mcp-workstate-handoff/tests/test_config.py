import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from workstate_handoff_mcp.config import RuntimeConfig, _resolve_primary_worktree_root
from workstate_handoff_mcp.review_findings_updates import _workspace_has_uncommitted_changes
from workstate_handoff_mcp.runtime import configure_runtime, reset_runtime_config
from workstate_handoff_mcp.shared_write_context import _workspace_git_context


def test_runtime_config_defaults_to_workspace_state() -> None:
    root = Path("/tmp/workstate-handoff").resolve()
    runtime = RuntimeConfig.for_workspace(root)

    assert runtime.workspace_root == root
    assert runtime.state_dir == root / ".task-state"
    assert runtime.db_path == root / ".task-state" / "handoff.db"
    assert runtime.current_task_path == root / "CURRENT_TASK.json"
    assert runtime.exports_dir == root / ".task-state" / "exports"


def test_runtime_config_default_tool_profile_is_all() -> None:
    root = Path("/tmp/workstate-handoff").resolve()
    runtime = RuntimeConfig.for_workspace(root)
    assert runtime.tool_profile == "all"


def test_runtime_config_rejects_legacy_core_tool_profile() -> None:
    root = Path("/tmp/workstate-handoff").resolve()
    with pytest.raises(ValueError, match="Invalid tool_profile"):
        RuntimeConfig.for_workspace(root, tool_profile="core")


def test_runtime_config_from_args_rejects_legacy_tool_profile_env() -> None:
    root = Path("/tmp/workstate-handoff").resolve()
    explicit_state = root / ".task-state"

    class FakeArgs:
        workspace_root = str(root)
        state_dir = str(explicit_state)
        current_task_path = None
        exports_dir = None
        tool_profile = None

    with mock.patch.dict(os.environ, {"WORKSTATE_HANDOFF_TOOL_PROFILE": "core"}):
        with pytest.raises(ValueError, match="Invalid tool_profile"):
            RuntimeConfig.from_args(FakeArgs())


def test_runtime_config_from_args_defaults_to_all() -> None:
    root = Path("/tmp/workstate-handoff").resolve()
    explicit_state = root / ".task-state"

    class FakeArgs:
        workspace_root = str(root)
        state_dir = str(explicit_state)
        current_task_path = None
        exports_dir = None
        tool_profile = None

    with mock.patch.dict(os.environ, {}, clear=True):
        os.environ["WORKSTATE_HANDOFF_WORKSPACE_ROOT"] = str(root)
        runtime = RuntimeConfig.from_args(FakeArgs())
    assert runtime.tool_profile == "all"


def test_runtime_config_from_args_rejects_legacy_cli_tool_profile() -> None:
    root = Path("/tmp/workstate-handoff").resolve()
    explicit_state = root / ".task-state"

    class FakeArgs:
        workspace_root = str(root)
        state_dir = str(explicit_state)
        current_task_path = None
        exports_dir = None
        tool_profile = "extended"

    with mock.patch.dict(os.environ, {"WORKSTATE_HANDOFF_TOOL_PROFILE": "core"}):
        with pytest.raises(ValueError, match="Invalid tool_profile"):
            RuntimeConfig.from_args(FakeArgs())


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def git_repo_with_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Create a primary git repo plus a linked worktree under tmp_path.

    Returns ``(primary_root, linked_root)``. The linked worktree is on a
    second branch checked out into a sibling directory, mirroring the
    monorepo's `make task-start` layout.
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    _run_git(primary, "init", "-q", "-b", "main")
    _run_git(primary, "config", "user.email", "test@example.com")
    _run_git(primary, "config", "user.name", "Test User")
    _run_git(primary, "commit", "--allow-empty", "-m", "init", "-q")
    linked = tmp_path / "primary-feature"
    _run_git(primary, "branch", "feature/test")
    _run_git(primary, "worktree", "add", "-q", str(linked), "feature/test")
    return primary, linked


@pytest.fixture
def git_repo_with_two_linked_worktrees(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create a primary git repo plus two linked worktrees under tmp_path."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _run_git(primary, "init", "-q", "-b", "main")
    _run_git(primary, "config", "user.email", "test@example.com")
    _run_git(primary, "config", "user.name", "Test User")
    _run_git(primary, "commit", "--allow-empty", "-m", "init", "-q")
    linked_one = tmp_path / "primary-feature-one"
    linked_two = tmp_path / "primary-feature-two"
    _run_git(primary, "branch", "feature/one")
    _run_git(primary, "branch", "feature/two")
    _run_git(primary, "worktree", "add", "-q", str(linked_one), "feature/one")
    _run_git(primary, "worktree", "add", "-q", str(linked_two), "feature/two")
    return primary, linked_one, linked_two


def test_for_repo_resolves_primary_root_from_primary_worktree(
    git_repo_with_linked_worktree: tuple[Path, Path],
) -> None:
    """When called from inside the primary worktree, for_repo() returns the
    primary worktree's root."""
    primary, _linked = git_repo_with_linked_worktree
    runtime = RuntimeConfig.for_repo(primary)
    assert runtime.workspace_root == primary.resolve()
    assert runtime.state_dir == primary.resolve() / ".task-state"
    assert runtime.db_path == primary.resolve() / ".task-state" / "handoff.db"


def test_for_repo_collapses_linked_worktree_to_primary_root(
    git_repo_with_linked_worktree: tuple[Path, Path],
) -> None:
    """When called from inside a linked worktree, for_repo() must still
    resolve to the primary worktree's root so all worktrees share a single
    handoff DB. This is the WORKSTATE-REF-16 fix."""
    primary, linked = git_repo_with_linked_worktree
    runtime = RuntimeConfig.for_repo(linked)
    assert runtime.workspace_root == primary.resolve()
    assert runtime.db_path == primary.resolve() / ".task-state" / "handoff.db"


def test_for_repo_falls_back_to_start_dir_outside_git(tmp_path: Path) -> None:
    """When start_dir is not inside any git repo, for_repo() falls back to
    using start_dir as the workspace root."""
    not_a_repo = tmp_path / "scratch"
    not_a_repo.mkdir()
    runtime = RuntimeConfig.for_repo(not_a_repo)
    assert runtime.workspace_root == not_a_repo.resolve()


def test_from_args_rejects_non_git_workspace_root_without_explicit_path_overrides(tmp_path: Path) -> None:
    """Packaged-consumer startup must fail fast outside git repos unless the
    caller explicitly anchors the runtime paths."""
    not_a_repo = tmp_path / "scratch"
    not_a_repo.mkdir()

    class FakeArgs:
        workspace_root = str(not_a_repo)
        state_dir = None
        current_task_path = None
        dashboard_path = None
        exports_dir = None
        tool_profile = None

    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="could not resolve <consumer-root>") as excinfo:
            RuntimeConfig.from_args(FakeArgs())

    assert excinfo.type.__name__ == "ConsumerRootResolutionError"
    message = str(excinfo.value)
    assert "WORKSTATE_HANDOFF_WORKSPACE_ROOT" in message
    assert "WORKSTATE_HANDOFF_STATE_DIR" in message
    assert "WORKSTATE_HANDOFF_DASHBOARD_PATH" in message
    assert "WORKSTATE_HANDOFF_CURRENT_TASK_PATH" in message


def test_from_args_allows_non_git_workspace_root_with_explicit_state_dir(tmp_path: Path) -> None:
    """Explicit path overrides remain the escape hatch for non-git fixtures
    and packaged-consumer setups that do not anchor at a repo root."""
    not_a_repo = tmp_path / "scratch"
    not_a_repo.mkdir()
    explicit_state = tmp_path / "explicit-state"

    class FakeArgs:
        workspace_root = str(not_a_repo)
        state_dir = str(explicit_state)
        current_task_path = None
        dashboard_path = None
        exports_dir = None
        tool_profile = None

    with mock.patch.dict(os.environ, {}, clear=True):
        runtime = RuntimeConfig.from_args(FakeArgs())

    assert runtime.workspace_root == not_a_repo.resolve()
    assert runtime.state_dir == explicit_state.resolve()
    assert runtime.db_path == explicit_state.resolve() / "handoff.db"


def test_for_repo_passes_through_explicit_state_dir(
    git_repo_with_linked_worktree: tuple[Path, Path], tmp_path: Path
) -> None:
    """An explicit state_dir override must take precedence over the
    primary-worktree resolution. This preserves the escape hatch for
    fixtures that anchor at a snapshotted state directory."""
    _primary, linked = git_repo_with_linked_worktree
    custom_state = tmp_path / "custom-state"
    runtime = RuntimeConfig.for_repo(linked, state_dir=custom_state)
    assert runtime.state_dir == custom_state.resolve()
    assert runtime.db_path == custom_state.resolve() / "handoff.db"


def test_for_workspace_resolves_relative_overrides_from_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root = tmp_path / "consumer"
    launcher_cwd = tmp_path / "launcher"
    workspace_root.mkdir()
    launcher_cwd.mkdir()
    monkeypatch.chdir(launcher_cwd)

    runtime = RuntimeConfig.for_workspace(
        workspace_root,
        state_dir=".task-state",
        current_task_path="CURRENT_TASK.json",
        dashboard_path="DASHBOARD.txt",
        exports_dir=".task-state/exports",
    )

    assert runtime.workspace_root == workspace_root.resolve()
    assert runtime.state_dir == workspace_root.resolve() / ".task-state"
    assert runtime.db_path == workspace_root.resolve() / ".task-state" / "handoff.db"
    assert runtime.current_task_path == workspace_root.resolve() / "CURRENT_TASK.json"
    assert runtime.dashboard_path == workspace_root.resolve() / "DASHBOARD.txt"
    assert runtime.exports_dir == workspace_root.resolve() / ".task-state" / "exports"


def test_resolve_primary_worktree_root_returns_none_for_missing_dir(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert _resolve_primary_worktree_root(missing) is None


def test_from_args_collapses_linked_worktree_workspace_root_to_primary(
    git_repo_with_linked_worktree: tuple[Path, Path],
) -> None:
    """WORKSTATE-REF-16-BR-01 regression: from_args() must route through for_repo()
    so an MCP server launched with --workspace-root pointing at a linked
    worktree binds to the primary worktree's .task-state/handoff.db.

    This is the structural divergence loop the WORKSTATE-REF-16 base slice claimed
    to close. Before the fix, from_args() called for_workspace() directly,
    so the linked-worktree workspace_root resolved to a fresh empty
    per-worktree DB, defeating the resolution that for_repo() does for
    the lifecycle scripts.
    """
    primary, linked = git_repo_with_linked_worktree

    class FakeArgs:
        workspace_root = str(linked)
        state_dir = None
        current_task_path = None
        exports_dir = None
        tool_profile = None

    with mock.patch.dict(os.environ, {}, clear=True):
        runtime = RuntimeConfig.from_args(FakeArgs())

    assert runtime.workspace_root == primary.resolve(), (
        "from_args must collapse a linked-worktree workspace_root to the primary "
        f"(got {runtime.workspace_root}, expected {primary.resolve()})"
    )
    assert runtime.db_path == primary.resolve() / ".task-state" / "handoff.db"
    assert runtime.current_task_path == primary.resolve() / "CURRENT_TASK.json"
    assert runtime.dashboard_path == primary.resolve() / "DASHBOARD.txt"
    assert runtime.exports_dir == primary.resolve() / ".task-state" / "exports"


def test_from_args_preserves_linked_worktree_git_root(
    git_repo_with_linked_worktree: tuple[Path, Path],
) -> None:
    primary, linked = git_repo_with_linked_worktree

    class FakeArgs:
        workspace_root = str(linked)
        state_dir = None
        current_task_path = None
        dashboard_path = None
        exports_dir = None
        tool_profile = None

    with mock.patch.dict(os.environ, {}, clear=True):
        runtime = RuntimeConfig.from_args(FakeArgs())

    assert runtime.workspace_root == primary.resolve()
    assert runtime.state_workspace_root == primary.resolve()
    assert runtime.git_workspace_root == linked.resolve()


def test_workspace_git_context_reads_linked_worktree_head(
    git_repo_with_linked_worktree: tuple[Path, Path],
) -> None:
    primary, linked = git_repo_with_linked_worktree
    (linked / "linked.txt").write_text("linked\n")
    _run_git(linked, "add", "linked.txt")
    _run_git(linked, "commit", "-m", "linked commit", "-q")
    linked_head = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    class FakeArgs:
        workspace_root = str(linked)
        state_dir = None
        current_task_path = None
        dashboard_path = None
        exports_dir = None
        tool_profile = None

    try:
        with mock.patch.dict(os.environ, {}, clear=True):
            configure_runtime(RuntimeConfig.from_args(FakeArgs()))
        context = _workspace_git_context()
    finally:
        reset_runtime_config()

    assert context["branch"] == "feature/test"
    assert context["commit_sha"] == linked_head
    assert (
        context["commit_sha"]
        != subprocess.run(
            ["git", "-C", str(primary), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )


def test_workspace_cleanliness_reads_linked_worktree_status(
    git_repo_with_linked_worktree: tuple[Path, Path],
) -> None:
    primary, linked = git_repo_with_linked_worktree
    (primary / "dirty-primary.txt").write_text("dirty\n")

    class FakeArgs:
        workspace_root = str(linked)
        state_dir = None
        current_task_path = None
        dashboard_path = None
        exports_dir = None
        tool_profile = None

    try:
        with mock.patch.dict(os.environ, {}, clear=True):
            configure_runtime(RuntimeConfig.from_args(FakeArgs()))
        cleanliness = _workspace_has_uncommitted_changes()
    finally:
        reset_runtime_config()

    assert cleanliness.error is None
    assert cleanliness.has_uncommitted_changes is False


@pytest.mark.parametrize(
    ("env_name", "attribute_name", "relative_path"),
    [
        ("WORKSTATE_HANDOFF_DASHBOARD_PATH", "dashboard_path", Path("artifacts") / "custom-dashboard.txt"),
        ("WORKSTATE_HANDOFF_CURRENT_TASK_PATH", "current_task_path", Path("artifacts") / "custom-current-task.json"),
        ("WORKSTATE_HANDOFF_EXPORTS_DIR", "exports_dir", Path("artifacts") / "exports"),
    ],
)
def test_from_args_honors_output_path_env_overrides(
    git_repo_with_linked_worktree: tuple[Path, Path],
    tmp_path: Path,
    env_name: str,
    attribute_name: str,
    relative_path: Path,
) -> None:
    primary, linked = git_repo_with_linked_worktree
    override_path = tmp_path / relative_path

    class FakeArgs:
        workspace_root = str(linked)
        state_dir = None
        current_task_path = None
        dashboard_path = None
        exports_dir = None
        tool_profile = None

    with mock.patch.dict(os.environ, {env_name: str(override_path)}, clear=True):
        runtime = RuntimeConfig.from_args(FakeArgs())

    assert runtime.workspace_root == primary.resolve()
    assert getattr(runtime, attribute_name) == override_path.resolve()


def test_for_repo_collapses_multiple_linked_worktrees_to_one_primary_root(
    git_repo_with_two_linked_worktrees: tuple[Path, Path, Path],
) -> None:
    primary, linked_one, linked_two = git_repo_with_two_linked_worktrees

    primary_runtime = RuntimeConfig.for_repo(primary)
    linked_one_runtime = RuntimeConfig.for_repo(linked_one)
    linked_two_runtime = RuntimeConfig.for_repo(linked_two)

    expected_root = primary.resolve()
    expected_state_dir = expected_root / ".task-state"
    expected_db_path = expected_state_dir / "handoff.db"
    expected_current_task_path = expected_root / "CURRENT_TASK.json"
    expected_dashboard_path = expected_root / "DASHBOARD.txt"
    expected_exports_dir = expected_state_dir / "exports"

    for runtime in (primary_runtime, linked_one_runtime, linked_two_runtime):
        assert runtime.workspace_root == expected_root
        assert runtime.state_dir == expected_state_dir
        assert runtime.db_path == expected_db_path
        assert runtime.current_task_path == expected_current_task_path
        assert runtime.dashboard_path == expected_dashboard_path
        assert runtime.exports_dir == expected_exports_dir


def test_from_args_preserves_explicit_state_dir_override(
    git_repo_with_linked_worktree: tuple[Path, Path], tmp_path: Path
) -> None:
    """WORKSTATE-REF-16-BR-01 escape hatch: an explicit --state-dir override remains
    authoritative even when from_args() is routing workspace_root through
    for_repo. Callers with a legitimate per-worktree-state use case (test
    snapshots, isolation fixtures) keep their override semantics."""
    primary, linked = git_repo_with_linked_worktree
    explicit_state = tmp_path / "explicit-state"

    class FakeArgs:
        workspace_root = str(linked)
        state_dir = str(explicit_state)
        current_task_path = None
        exports_dir = None
        tool_profile = None

    with mock.patch.dict(os.environ, {}, clear=True):
        runtime = RuntimeConfig.from_args(FakeArgs())

    # workspace_root still collapses to the primary worktree (the structural
    # default), but the explicit state_dir override is honored byte-for-byte.
    assert runtime.workspace_root == primary.resolve()
    assert runtime.state_dir == explicit_state.resolve()
    assert runtime.db_path == explicit_state.resolve() / "handoff.db"


def test_from_args_resolves_relative_overrides_from_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root = tmp_path / "consumer"
    launcher_cwd = tmp_path / "launcher"
    workspace_root.mkdir()
    launcher_cwd.mkdir()
    monkeypatch.chdir(launcher_cwd)

    class FakeArgs:
        pass

    FakeArgs.workspace_root = str(workspace_root)
    FakeArgs.state_dir = ".task-state"
    FakeArgs.current_task_path = "CURRENT_TASK.json"
    FakeArgs.dashboard_path = "DASHBOARD.txt"
    FakeArgs.exports_dir = ".task-state/exports"
    FakeArgs.tool_profile = None

    with mock.patch.dict(os.environ, {}, clear=True):
        runtime = RuntimeConfig.from_args(FakeArgs())

    assert runtime.workspace_root == workspace_root.resolve()
    assert runtime.state_dir == workspace_root.resolve() / ".task-state"
    assert runtime.db_path == workspace_root.resolve() / ".task-state" / "handoff.db"
    assert runtime.current_task_path == workspace_root.resolve() / "CURRENT_TASK.json"
    assert runtime.dashboard_path == workspace_root.resolve() / "DASHBOARD.txt"
    assert runtime.exports_dir == workspace_root.resolve() / ".task-state" / "exports"


def test_runtime_config_rejects_invalid_tool_profile() -> None:
    root = Path("/tmp/workstate-handoff").resolve()
    explicit_state = root / ".task-state"

    with mock.patch.dict(os.environ, {"WORKSTATE_HANDOFF_TOOL_PROFILE": "invalid"}):
        with mock.patch.dict(os.environ, {"WORKSTATE_HANDOFF_WORKSPACE_ROOT": str(root)}, clear=False):

            class FakeArgs:
                workspace_root = str(root)
                state_dir = str(explicit_state)
                current_task_path = None
                exports_dir = None
                tool_profile = None

            with pytest.raises(ValueError, match="Invalid tool_profile"):
                RuntimeConfig.from_args(FakeArgs())


# WORKSTATE-REF-68 implementation note: finding_lifecycle_states_enabled feature-flag plumbing.
# Follows the current_task_auto_regen precedent: default off, env-var
# override via WORKSTATE_HANDOFF_FINDING_LIFECYCLE_STATES, constructor kwarg
# override on for_workspace / for_repo, round-trip through from_args via the
# env var.


def test_finding_lifecycle_states_default_is_on() -> None:
    """WORKSTATE-REF-68 implementation note: rollout complete — the default is now ``True``.

    Operators that need to fall back to the legacy single-state behavior must
    pass ``finding_lifecycle_states_enabled=False`` explicitly or set
    ``WORKSTATE_HANDOFF_FINDING_LIFECYCLE_STATES=0``."""
    root = Path("/tmp/workstate-handoff").resolve()
    with mock.patch.dict(os.environ, {}, clear=True):
        runtime = RuntimeConfig.for_workspace(root)
    assert runtime.finding_lifecycle_states_enabled is True


def test_finding_lifecycle_states_env_var_opts_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators flip the flag via WORKSTATE_HANDOFF_FINDING_LIFECYCLE_STATES."""
    root = Path("/tmp/workstate-handoff").resolve()
    monkeypatch.setenv("WORKSTATE_HANDOFF_FINDING_LIFECYCLE_STATES", "1")
    runtime = RuntimeConfig.for_workspace(root)
    assert runtime.finding_lifecycle_states_enabled is True

    # Falsy values stay off (parity with current_task_auto_regen semantics).
    for falsy in ("0", "false", "off", "no"):
        monkeypatch.setenv("WORKSTATE_HANDOFF_FINDING_LIFECYCLE_STATES", falsy)
        runtime = RuntimeConfig.for_workspace(root)
        assert runtime.finding_lifecycle_states_enabled is False, falsy


def test_finding_lifecycle_states_constructor_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit kwarg beats env var (matches current_task_auto_regen)."""
    root = Path("/tmp/workstate-handoff").resolve()
    monkeypatch.setenv("WORKSTATE_HANDOFF_FINDING_LIFECYCLE_STATES", "0")
    runtime = RuntimeConfig.for_workspace(root, finding_lifecycle_states_enabled=True)
    assert runtime.finding_lifecycle_states_enabled is True

    monkeypatch.setenv("WORKSTATE_HANDOFF_FINDING_LIFECYCLE_STATES", "1")
    runtime = RuntimeConfig.for_workspace(root, finding_lifecycle_states_enabled=False)
    assert runtime.finding_lifecycle_states_enabled is False


def test_finding_lifecycle_states_round_trip_through_from_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from_args picks up the env var via for_repo -> for_workspace."""
    root = Path("/tmp/workstate-handoff").resolve()
    explicit_state = root / ".task-state"

    class FakeArgs:
        workspace_root = str(root)
        state_dir = str(explicit_state)
        current_task_path = None
        dashboard_path = None
        exports_dir = None
        tool_profile = None

    monkeypatch.setenv("WORKSTATE_HANDOFF_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("WORKSTATE_HANDOFF_FINDING_LIFECYCLE_STATES", "1")
    runtime = RuntimeConfig.from_args(FakeArgs())
    assert runtime.finding_lifecycle_states_enabled is True

    monkeypatch.setenv("WORKSTATE_HANDOFF_FINDING_LIFECYCLE_STATES", "0")
    runtime = RuntimeConfig.from_args(FakeArgs())
    assert runtime.finding_lifecycle_states_enabled is False

    monkeypatch.delenv("WORKSTATE_HANDOFF_FINDING_LIFECYCLE_STATES")
    runtime = RuntimeConfig.from_args(FakeArgs())
    assert runtime.finding_lifecycle_states_enabled is True
