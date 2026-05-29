"""WORKSTATE-REF-65 implementation note integration: ``_common._live_task_refs`` + resolver.

Drives the helper with a real in-process ``HANDOFF_DB`` (via
``RuntimeConfig.for_workspace`` + ``set_handoff_state``) so the
registered-ref selector is exercised end-to-end:

- **Case A**: both ``WORKSTATE-REF-63`` and ``WORKSTATE-REF-63-FU-EXAMPLE`` live →
  resolution of ``feature/WORKSTATE-63-fu-example`` returns the follow-up.
- **Case B**: only ``WORKSTATE-REF-63`` live → same branch resolves to the base
  (graceful "longest registered wins" with a singleton intersection).
- **Case C**: ``WORKSTATE-REF-63-FU-EXAMPLE`` is ``status=done`` → excluded at
  the read boundary by ``status_filter=LIVE_ACTIVE_STATUSES``; base
  wins. Proves ``done``-rows cannot steal a follow-up resolution.
- **Case D**: registry unreachable (runtime not configured) → helper
  returns ``set()``, selector falls back to shortest-prefix, no crash.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "scripts" / "workstate" / "lifecycle"
HANDLERS_DIR = LIFECYCLE_PKG / "handlers"
HANDOFF_SRC = (PACKAGE_ROOT.parent / "mcp-workstate-handoff" / "src").resolve()

if str(HANDOFF_SRC) not in sys.path:
    sys.path.insert(0, str(HANDOFF_SRC))

from workstate_handoff_mcp import api as mcp_server  # noqa: WORKSTATE-REF-402
from workstate_handoff_mcp.config import RuntimeConfig  # noqa: WORKSTATE-REF-402


@pytest.fixture
def lifecycle_modules():
    """Load ``resolver`` and ``handlers._common`` fresh per test."""
    saved_path = list(sys.path)
    saved_modules = {
        name: sys.modules.get(name)
        for name in ("resolver", "handlers", "handlers._common")
    }
    sys.path.insert(0, str(LIFECYCLE_PKG))
    try:
        for name in ("resolver", "handlers", "handlers._common"):
            sys.modules.pop(name, None)

        import resolver as resolver_mod  # type: ignore[import-not-found]

        handlers_init = HANDLERS_DIR / "__init__.py"
        spec_pkg = importlib.util.spec_from_file_location(
            "handlers", handlers_init, submodule_search_locations=[str(HANDLERS_DIR)]
        )
        assert spec_pkg is not None
        pkg = importlib.util.module_from_spec(spec_pkg)
        sys.modules["handlers"] = pkg
        assert spec_pkg.loader is not None
        spec_pkg.loader.exec_module(pkg)

        common_path = HANDLERS_DIR / "_common.py"
        spec_common = importlib.util.spec_from_file_location(
            "handlers._common", common_path
        )
        assert spec_common is not None
        common_mod = importlib.util.module_from_spec(spec_common)
        sys.modules["handlers._common"] = common_mod
        assert spec_common.loader is not None
        spec_common.loader.exec_module(common_mod)

        yield resolver_mod, common_mod
    finally:
        sys.path[:] = saved_path
        for name, module in saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", "init", "-q", "-b", "feature/WORKSTATE-63-fu-example", str(repo)],
        check=True, env=env,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"],
        check=True, env=env,
    )
    return repo


def _configure(repo: Path) -> None:
    runtime = RuntimeConfig.for_workspace(
        repo,
        state_dir=repo / ".task-state",
        current_task_path=repo / "CURRENT_TASK.json",
        dashboard_path=repo / "DASHBOARD.txt",
    )
    mcp_server.configure_runtime(runtime)


def _seed(task_ref: str, repo: Path, *, status: str = "in_progress") -> None:
    mcp_server.set_handoff_state(
        task_ref=task_ref,
        objective=f"{task_ref} integration fixture",
        status=status,
        target_branch="feature/WORKSTATE-63-fu-example",
        target_worktree_path=str(repo),
        task_plan_path=f"docs/tasks/{task_ref}.md",
    )


def test_case_a_both_live_follow_up_wins(
    lifecycle_modules, git_repo: Path
) -> None:
    resolver_mod, common_mod = lifecycle_modules
    _configure(git_repo)
    try:
        _seed("WORKSTATE-REF-63", git_repo)
        _seed("WORKSTATE-REF-63-FU-EXAMPLE", git_repo)

        known = common_mod._live_task_refs(git_repo)
        assert "WORKSTATE-REF-63" in known
        assert "WORKSTATE-REF-63-FU-EXAMPLE" in known

        resolved = resolver_mod.derive_task_ref(
            "feature/WORKSTATE-63-fu-example", known_task_refs=known
        )
        assert resolved == "WORKSTATE-REF-63-FU-EXAMPLE"
    finally:
        mcp_server.reset_runtime_config()


def test_case_b_only_base_live_base_wins(
    lifecycle_modules, git_repo: Path
) -> None:
    resolver_mod, common_mod = lifecycle_modules
    _configure(git_repo)
    try:
        _seed("WORKSTATE-REF-63", git_repo)

        known = common_mod._live_task_refs(git_repo)
        assert known == {"WORKSTATE-REF-63"}

        resolved = resolver_mod.derive_task_ref(
            "feature/WORKSTATE-63-fu-example", known_task_refs=known
        )
        assert resolved == "WORKSTATE-REF-63"
    finally:
        mcp_server.reset_runtime_config()


def test_case_c_done_status_excluded_at_read_boundary(
    lifecycle_modules, git_repo: Path
) -> None:
    """``status_filter=LIVE_ACTIVE_STATUSES`` excludes ``done`` rows at the
    read boundary; a done base cannot steal a live follow-up's
    resolution, and conversely a done follow-up cannot steal the live
    base's resolution."""
    resolver_mod, common_mod = lifecycle_modules
    _configure(git_repo)
    try:
        _seed("WORKSTATE-REF-63", git_repo, status="in_progress")
        _seed("WORKSTATE-REF-63-FU-EXAMPLE", git_repo, status="in_progress")
        # Flip the follow-up to done — should drop from live registry.
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-63-FU-EXAMPLE",
            status="done",
            status_only=True,
        )

        known = common_mod._live_task_refs(git_repo)
        assert known == {"WORKSTATE-REF-63"}, known

        resolved = resolver_mod.derive_task_ref(
            "feature/WORKSTATE-63-fu-example", known_task_refs=known
        )
        assert resolved == "WORKSTATE-REF-63"
    finally:
        mcp_server.reset_runtime_config()


def test_case_br_01_subprocess_path_works_without_inprocess_runtime(
    lifecycle_modules, git_repo: Path
) -> None:
    """WORKSTATE65-BR-01 regression: the helper must discover live rows even
    when the *in-process* handoff runtime is not configured at the
    moment of the call. Lifecycle commands run as fresh CLI processes
    without inheriting any in-process configure_runtime — the previous
    in-process ``list_handoff_rows`` import path returned ``set()`` in
    that scenario, silently bypassing the WORKSTATE-REF-65 fix in production.
    The subprocess CLI path resolves its own runtime from
    ``--workspace-root``, so live rows persisted to disk must surface
    here.
    """
    resolver_mod, common_mod = lifecycle_modules
    # Seed via a temporary in-process configuration that we then tear
    # down before measuring — mimicking the production sequence where
    # ``set_handoff_state`` was called by some earlier MCP server and
    # the lifecycle CLI is later launched as a separate process.
    _configure(git_repo)
    _seed("WORKSTATE-REF-63", git_repo)
    _seed("WORKSTATE-REF-63-FU-EXAMPLE", git_repo)
    mcp_server.reset_runtime_config()

    # No active in-process runtime — but the subprocess CLI configures
    # its own from ``--workspace-root`` and reads the same on-disk DB.
    known = common_mod._live_task_refs(git_repo)
    assert "WORKSTATE-REF-63" in known, known
    assert "WORKSTATE-REF-63-FU-EXAMPLE" in known, known

    resolved = resolver_mod.derive_task_ref(
        "feature/WORKSTATE-63-fu-example", known_task_refs=known
    )
    assert resolved == "WORKSTATE-REF-63-FU-EXAMPLE"


def test_case_br_02_nonempty_registry_no_intersection_returns_none(
    lifecycle_modules, git_repo: Path
) -> None:
    """WORKSTATE65-BR-02 regression: when the registry is non-empty but no
    branch-name candidate intersects with it, the resolver must return
    ``None`` instead of naming a candidate absent from the populated
    registry. The shortest-prefix fallback applies only to the
    empty-registry / no-context case (Case D)."""
    resolver_mod, _common_mod = lifecycle_modules
    # Populated registry with unrelated refs only — branch
    # ``feature/WORKSTATE-63-fu-example`` has candidates
    # WORKSTATE-REF-63-FU-EXAMPLE / WORKSTATE-REF-63 / (no further digit-bearing
    # prefixes), none of which appear in the registry.
    known = {"WORKSTATE-REF-02", "WORKSTATE-REF-99"}
    resolved = resolver_mod.derive_task_ref(
        "feature/WORKSTATE-63-fu-example", known_task_refs=known
    )
    assert resolved is None


def test_case_d_empty_registry_degrades_gracefully(
    lifecycle_modules, git_repo: Path
) -> None:
    """When the handoff registry has never been seeded for this worktree
    (no live rows in the DB), ``_live_task_refs`` returns ``set()`` and
    the resolver degrades to shortest-prefix. WORKSTATE65-BR-01: the helper
    now shells out to the canonical ``handoff-rows`` CLI, so the
    in-process runtime does not need to be configured for the subprocess
    to answer; WORKSTATE65-BR-02: an *empty* registry is the no-context
    fallback (shortest-prefix), distinct from a non-empty registry with
    no intersection (``None``)."""
    resolver_mod, common_mod = lifecycle_modules
    # No _configure() / no _seed() — registry has no live rows for this
    # worktree. The CLI subprocess still configures its own runtime from
    # ``--workspace-root`` and returns ``[]``.
    mcp_server.reset_runtime_config()

    known = common_mod._live_task_refs(git_repo)
    assert known == set()

    resolved = resolver_mod.derive_task_ref(
        "feature/WORKSTATE-63-fu-example", known_task_refs=known
    )
    # Shortest-prefix fallback preserves today's no-context behavior.
    assert resolved == "WORKSTATE-REF-63"
