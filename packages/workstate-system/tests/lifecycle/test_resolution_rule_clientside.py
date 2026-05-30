"""Client-side four-step Resolution Rule contract — per-reader, v1+v2.

WORKSTATE-REF-54 implementation note sub-implementation note.5: each lifecycle reader that consumes
``CURRENT_TASK.json`` must accept BOTH the legacy ``schema_version: 1``
single-active shape and the forthcoming ``schema_version: 2``
derive-on-read shape (``single`` / ``workspace_ambiguous`` / ``none``).

Shared invariant under test:

- ``shape == "single"``  → the reader returns the active task.
- ``shape == "none"``    → the reader returns the empty/None sentinel.
- ``shape == "workspace_ambiguous"`` → the reader returns the empty
  sentinel (no last-writer-wins). Caller-side ambiguity guards (implementation note) are responsible for surfacing the ambiguity loudly; the reader
  itself must not silently bind to one of the candidate tasks.

Each reader gets a parameterized matrix over (3 shapes × 2 schema
versions) = 6 cases. The matrix is intentionally explicit — a missing
shape × schema_version pair would let one branch of the compat reader
quietly degrade in production. Tests live in this single file so the
"5 readers × 3 shapes × 2 schema_versions = 30 cases" proof point
called out in the WORKSTATE-REF-54 plan can be read off one ``pytest -v``
invocation.

This sub-slice migrates the readers one at a time (one commit each,
for rollback granularity); cases land alongside their owning reader
edit so neither schema version is left unverified at any
slice-commit boundary.
"""

from __future__ import annotations

import importlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "scripts" / "workstate" / "lifecycle"
HANDLERS_PKG = LIFECYCLE_PKG / "handlers"


def _load_handlers_module(module_basename: str) -> Any:
    """Import a lifecycle handler module standalone for in-process tests.

    The lifecycle CLI normally executes via ``python <LIFECYCLE_PKG>``,
    which puts ``LIFECYCLE_PKG`` on sys.path implicitly. For in-process
    tests we replicate that so ``handlers._common`` and ``resolver``
    resolve the same way the running CLI does.
    """
    if str(LIFECYCLE_PKG) not in sys.path:
        sys.path.insert(0, str(LIFECYCLE_PKG))
    # Drop cached copies so each test sees a fresh import; otherwise a
    # prior test that monkeypatched ``handlers._common`` would leak.
    for cached in (
        f"handlers.{module_basename}",
        "handlers._common",
        "handlers",
        "resolver",
    ):
        sys.modules.pop(cached, None)
    return importlib.import_module(f"handlers.{module_basename}")


# ---------------------------------------------------------------------------
# Fixture builders: emit the two writer-shapes the compat reader accepts.
#
# v1 mirrors ``_build_current_task_file_payload`` in
# ``current_task_rendering.py`` (the live writer today). v2 mirrors
# ``_render_workspace_summary_from_per_task_files`` (the implementation note
# writer flip target). We hand-roll the payloads here instead of
# importing the writers so a writer-side regression cannot mask a
# reader-side regression.
# ---------------------------------------------------------------------------


def _v1_single_payload(task_ref: str, *, plan_path: str | None = None) -> dict:
    active: dict = {
        "task_ref": task_ref,
        "status": "in_progress",
        "objective": f"objective-{task_ref}",
        "focus": f"focus-{task_ref}",
        "target_branch": f"feature/{task_ref.lower()}",
        "target_worktree_path": f"/tmp/wt-{task_ref}",
        "revision": 1,
    }
    if plan_path is not None:
        active["task_plan_path"] = plan_path
    return {"schema_version": 1, "task_ref": task_ref, "active": active}


def _v1_none_payload() -> dict:
    return {"schema_version": 1, "task_ref": None, "active": None}


def _v2_single_payload(task_ref: str, *, plan_path: str | None = None) -> dict:
    active: dict = {
        "task_projection_schema_version": 1,
        "task_ref": task_ref,
        "status": "in_progress",
        "objective": f"objective-{task_ref}",
        "focus": f"focus-{task_ref}",
        "target_branch": f"feature/{task_ref.lower()}",
        "target_worktree_path": f"/tmp/wt-{task_ref}",
        "revision": 1,
        "updated_at": "2026-05-10T00:00:00Z",
    }
    if plan_path is not None:
        active["task_plan_path"] = plan_path
    return {
        "schema_version": 2,
        "shape": "single",
        "task_ref": task_ref,
        "active": active,
    }


def _v2_none_payload() -> dict:
    return {"schema_version": 2, "shape": "none"}


def _v2_workspace_ambiguous_payload(task_refs: list[str]) -> dict:
    return {
        "schema_version": 2,
        "shape": "workspace_ambiguous",
        "tasks": [
            {
                "task_projection_schema_version": 1,
                "task_ref": ref,
                "status": "in_progress",
                "objective": f"objective-{ref}",
                "focus": f"focus-{ref}",
                "target_branch": f"feature/{ref.lower()}",
                "target_worktree_path": f"/tmp/wt-{ref}",
                "revision": 1,
                "updated_at": "2026-05-10T00:00:00Z",
            }
            for ref in task_refs
        ],
    }


# v1 has no native ``workspace_ambiguous`` shape — the legacy writer
# always emits a single-active block. The compat reader's contract for
# v1 is therefore "single or none"; the third row in v1's matrix is
# *the absence of the active block* (which the compat reader normalizes
# to ``shape == "none"``). We enumerate it explicitly so the matrix is
# uniform across schema versions.
def _v1_pseudo_ambiguous_payload() -> dict:
    return _v1_none_payload()


# (shape_label, payload_factory) parameter pairs. ``payload_factory``
# accepts the task_ref(s) and returns the JSON-serializable dict.
SHAPE_MATRIX = [
    pytest.param("single", "v1", id="v1-single"),
    pytest.param("none", "v1", id="v1-none"),
    pytest.param("workspace_ambiguous", "v1", id="v1-pseudo-ambiguous"),
    pytest.param("single", "v2", id="v2-single"),
    pytest.param("none", "v2", id="v2-none"),
    pytest.param("workspace_ambiguous", "v2", id="v2-ambiguous"),
]


def _build_payload(shape: str, schema_version: str, task_ref: str) -> dict:
    if schema_version == "v1":
        if shape == "single":
            return _v1_single_payload(task_ref, plan_path=f"docs/plans/{task_ref}.md")
        return _v1_pseudo_ambiguous_payload()
    if shape == "single":
        return _v2_single_payload(task_ref, plan_path=f"docs/plans/{task_ref}.md")
    if shape == "none":
        return _v2_none_payload()
    return _v2_workspace_ambiguous_payload([task_ref, "WORKSTATE-REF-OTHER"])


# ---------------------------------------------------------------------------
# Reader 1: lifecycle ``context`` handler — ``_read_active_state``.
# Drives the handler end-to-end via subprocess so the JSON receipt's
# ``task_ref`` / ``plan_path`` fields reflect what the reader actually
# returned. Branch is non-conforming so the receipt's ``task_ref`` is
# entirely sourced from the snapshot rather than from branch derivation
# — that pins the reader's contribution to the receipt.
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _write_fake_cli(target: Path, body: str) -> None:
    target.write_text(body)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "--allow-empty", "-m", "init", "-q",
        ],
        check=True,
    )
    return repo


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    cli_dir = tmp_path / "fake-cli"
    cli_dir.mkdir()
    bin_path = cli_dir / "mcp-workstate-handoff"
    # Default fake CLI: every command exits 1. Tests that need the
    # rendered envelope rewrite this body via ``_install_envelope_cli``.
    _write_fake_cli(bin_path, "#!/usr/bin/env bash\nexit 1\n")
    return bin_path


def _envelope_for(payload: dict) -> dict:
    """Wrap a workspace-summary ``payload`` in a render-handoff envelope."""
    return {
        "schema_version": 2,
        "tool": "render_handoff",
        "ok": True,
        "data": {
            "task_ref": payload.get("task_ref"),
            "path": "/tmp/CURRENT_TASK.json",
            "written": False,
            "current_task_json": json.dumps(payload),
        },
    }


def _install_envelope_cli(fake_cli: Path, payload: dict) -> None:
    """Rewrite ``fake_cli`` so render-handoff returns the wrapped payload.

    WORKSTATE-REF-54-FU implementation note: the four lifecycle readers consume
    ``render-handoff --kind=current_task --no-write`` instead of reading
    ``CURRENT_TASK.json``. The fake CLI must echo the same content that
    used to live on disk — wrapped in a render-handoff envelope — so
    end-to-end subprocess-driven shape-matrix tests keep covering both
    schema versions.
    """
    envelope = _envelope_for(payload)
    body = (
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"render-handoff"* ]]; then\n'
        f"  cat <<'ENVELOPE_EOF'\n{json.dumps(envelope)}\nENVELOPE_EOF\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    _write_fake_cli(fake_cli, body)


def _patch_run_subprocess_with_envelope(
    monkeypatch: pytest.MonkeyPatch,
    common_mod: Any,
    payload: dict,
) -> list[list[str]]:
    """Patch ``_common.run_subprocess`` to answer render-handoff in-process.

    Used by the in-process reader tests (task_start, task_finish,
    shell_out): they call the reader helpers directly rather than
    shelling out, so the CLI substitution happens at the subprocess
    boundary inside ``_common``.
    """
    envelope = _envelope_for(payload)
    captured: list[list[str]] = []

    def _stub(argv: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        captured.append(list(argv))
        if any("render-handoff" in token for token in argv):
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout=json.dumps(envelope),
                stderr="",
            )
        return subprocess.CompletedProcess(args=argv, returncode=127, stdout="", stderr="")

    monkeypatch.setattr(common_mod, "run_subprocess", _stub)
    return captured


def _run_context(cwd: Path, fake_cli: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = str(fake_cli)
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "context", "--json"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.mark.parametrize("shape,schema_version", SHAPE_MATRIX)
def test_context_reader_accepts_v1_and_v2_payloads(
    git_repo: Path,
    fake_cli: Path,
    shape: str,
    schema_version: str,
) -> None:
    """``context._read_active_state`` must consume v1 + v2 payloads.

    Per-shape contract:

    - ``single``: ``plan_path`` populated from the snapshot.
    - ``none``: ``plan_path == None`` (no snapshot to read).
    - ``workspace_ambiguous``: ``plan_path == None`` (the reader does
      NOT pick a winner; implementation note's ambiguity guard is responsible).

    The test branch is non-conforming so the receipt's ``task_ref``
    cannot be branch-derived — the snapshot read is the only source.
    """
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "fix/non-conforming"],
        check=True,
    )
    payload = _build_payload(shape, schema_version, "WORKSTATE-REF-77")
    # WORKSTATE-REF-54-FU implementation note: the reader derives via render-handoff; surface
    # the payload through the fake CLI envelope rather than on disk.
    _install_envelope_cli(fake_cli, payload)

    proc = _run_context(git_repo, fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    if shape == "single":
        assert receipt["plan_path"] == "docs/plans/WORKSTATE-REF-77.md", receipt
    else:
        # ``none`` and ``workspace_ambiguous`` both yield no
        # snapshot-derived plan path. Branch is non-conforming so the
        # branch-derivation path also yields no task_ref.
        assert receipt["plan_path"] is None, (shape, schema_version, receipt)
        assert receipt["task_ref"] is None, (shape, schema_version, receipt)


def test_context_reader_routes_through_derive_workspace_summary_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``context._read_active_state`` MUST derive on read.

    WORKSTATE-REF-54-FU implementation note: the reader is required to go through
    ``handlers._common.derive_workspace_summary_view`` rather than open
    ``CURRENT_TASK.json`` directly. We assert by spying on the helper
    and confirming the handler hits it; an inline regression that bypasses
    the helper (and silently trusts the on-disk file) would fail.
    """
    context_mod = _load_handlers_module("context")
    common_mod = importlib.import_module("handlers._common")

    sentinel_calls: list[Any] = []
    real_derive = common_mod.derive_workspace_summary_view

    def _spy(repo: Any) -> Any:
        sentinel_calls.append(repo)
        return real_derive(repo)

    monkeypatch.setattr(common_mod, "derive_workspace_summary_view", _spy)
    if hasattr(context_mod, "derive_workspace_summary_view"):
        monkeypatch.setattr(context_mod, "derive_workspace_summary_view", _spy)

    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_run_subprocess_with_envelope(
        monkeypatch,
        common_mod,
        _v1_single_payload("WORKSTATE-REF-77", plan_path="docs/plans/WORKSTATE-REF-77.md"),
    )

    context_mod._read_active_state(repo)
    assert sentinel_calls, (
        "context._read_active_state did not invoke "
        "handlers._common.derive_workspace_summary_view — the reader "
        "is still on the on-disk-file path. Migrate it through the "
        "derive-on-read helper (WORKSTATE-REF-54-FU implementation note)."
    )


# ---------------------------------------------------------------------------
# Reader 2: lifecycle ``task-start`` handler — ``_read_active_state``.
# ``task-start`` runs heavy side effects (branch creation, worktree
# spawn, MCP set_handoff_state); driving the full handler in tests is
# out of scope for the reader contract. We exercise the helper directly
# in-process (matches the resolver-test pattern) and pin two facts:
#
#   1. The reader returns the active block for ``shape == "single"`` and
#      empty dict otherwise (no last-writer-wins on ambiguous).
#   2. The reader routes through ``load_workspace_summary_compat``.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape,schema_version", SHAPE_MATRIX)
def test_task_start_reader_accepts_v1_and_v2_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    schema_version: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _build_payload(shape, schema_version, "WORKSTATE-REF-77")

    task_start_mod = _load_handlers_module("task_start")
    common_mod = importlib.import_module("handlers._common")
    _patch_run_subprocess_with_envelope(monkeypatch, common_mod, payload)
    result = task_start_mod._read_active_state(repo)

    if shape == "single":
        assert result.get("task_ref") == "WORKSTATE-REF-77", (shape, schema_version, result)
        assert result.get("task_plan_path") == "docs/plans/WORKSTATE-REF-77.md", result
    else:
        # ``none`` and ``workspace_ambiguous`` both yield ``{}``. The
        # task-start ambiguity guard (implementation note) is the loud surface.
        assert result == {}, (shape, schema_version, result)


def test_task_start_reader_routes_through_derive_workspace_summary_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same structural contract as context: prove the wire-up explicitly
    so a future inline regression cannot pass the behavior matrix by
    accident (v1 single and v2 single payloads are field-equivalent at
    the envelope level)."""
    task_start_mod = _load_handlers_module("task_start")
    common_mod = importlib.import_module("handlers._common")

    sentinel_calls: list[Any] = []
    real_derive = common_mod.derive_workspace_summary_view

    def _spy(repo: Any) -> Any:
        sentinel_calls.append(repo)
        return real_derive(repo)

    monkeypatch.setattr(common_mod, "derive_workspace_summary_view", _spy)
    if hasattr(task_start_mod, "derive_workspace_summary_view"):
        monkeypatch.setattr(task_start_mod, "derive_workspace_summary_view", _spy)

    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_run_subprocess_with_envelope(
        monkeypatch,
        common_mod,
        _v1_single_payload("WORKSTATE-REF-77", plan_path="docs/plans/WORKSTATE-REF-77.md"),
    )

    task_start_mod._read_active_state(repo)
    assert sentinel_calls, (
        "task_start._read_active_state did not invoke "
        "handlers._common.derive_workspace_summary_view — the reader "
        "is still on the on-disk-file path. Migrate it through the "
        "derive-on-read helper (WORKSTATE-REF-54-FU implementation note)."
    )


# ---------------------------------------------------------------------------
# Reader 3: lifecycle ``task-finish`` handler — ``_read_active_task_ref``.
# Returns ``str | None`` (not a dict). Same shape contract as the dict
# readers: ``single`` yields the task_ref, ``workspace_ambiguous`` and
# ``none`` yield ``None``. The reader feeds task-finish's archive
# selection — under v2 ambiguous it must NOT pick a winner; the
# operator-supplied ``--task`` flag is the disambiguation surface.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape,schema_version", SHAPE_MATRIX)
def test_task_finish_reader_accepts_v1_and_v2_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    schema_version: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _build_payload(shape, schema_version, "WORKSTATE-REF-77")

    task_finish_mod = _load_handlers_module("task_finish")
    common_mod = importlib.import_module("handlers._common")
    _patch_run_subprocess_with_envelope(monkeypatch, common_mod, payload)
    result = task_finish_mod._read_active_task_ref(repo)

    if shape == "single":
        assert result == "WORKSTATE-REF-77", (shape, schema_version, result)
    else:
        assert result is None, (shape, schema_version, result)


def test_task_finish_reader_routes_through_derive_workspace_summary_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_finish_mod = _load_handlers_module("task_finish")
    common_mod = importlib.import_module("handlers._common")

    sentinel_calls: list[Any] = []
    real_derive = common_mod.derive_workspace_summary_view

    def _spy(repo: Any) -> Any:
        sentinel_calls.append(repo)
        return real_derive(repo)

    monkeypatch.setattr(common_mod, "derive_workspace_summary_view", _spy)
    if hasattr(task_finish_mod, "derive_workspace_summary_view"):
        monkeypatch.setattr(task_finish_mod, "derive_workspace_summary_view", _spy)

    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_run_subprocess_with_envelope(monkeypatch, common_mod, _v1_single_payload("WORKSTATE-REF-77"))

    task_finish_mod._read_active_task_ref(repo)
    assert sentinel_calls, (
        "task_finish._read_active_task_ref did not invoke "
        "handlers._common.derive_workspace_summary_view — the reader "
        "is still on the on-disk-file path. Migrate it through the "
        "derive-on-read helper (WORKSTATE-REF-54-FU implementation note)."
    )


# ---------------------------------------------------------------------------
# Reader 4: lifecycle ``shell_out`` handler — ``_read_active_task_ref``.
# Same return shape as task_finish (``str | None``) but takes an
# optional ``workspace_root`` (the wrapper passes None when the
# operator did not register one). The fallback is the operator-supplied
# ``--task-ref`` flag, so under ambiguity the reader returns None and
# the wrapper falls through to the explicit flag.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape,schema_version", SHAPE_MATRIX)
def test_shell_out_reader_accepts_v1_and_v2_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    schema_version: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _build_payload(shape, schema_version, "WORKSTATE-REF-77")

    shell_out_mod = _load_handlers_module("shell_out")
    common_mod = importlib.import_module("handlers._common")
    _patch_run_subprocess_with_envelope(monkeypatch, common_mod, payload)
    result = shell_out_mod._read_active_task_ref(repo)

    if shape == "single":
        assert result == "WORKSTATE-REF-77", (shape, schema_version, result)
    else:
        assert result is None, (shape, schema_version, result)


def test_shell_out_reader_handles_none_workspace_root() -> None:
    """``shell_out._read_active_task_ref`` accepts ``None`` to mean "no
    operator-registered workspace"; that path predates WORKSTATE-REF-54 and
    must keep returning ``None`` regardless of compat-reader migration."""
    shell_out_mod = _load_handlers_module("shell_out")
    assert shell_out_mod._read_active_task_ref(None) is None


def test_shell_out_reader_routes_through_derive_workspace_summary_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell_out_mod = _load_handlers_module("shell_out")
    common_mod = importlib.import_module("handlers._common")

    sentinel_calls: list[Any] = []
    real_derive = common_mod.derive_workspace_summary_view

    def _spy(repo: Any) -> Any:
        sentinel_calls.append(repo)
        return real_derive(repo)

    monkeypatch.setattr(common_mod, "derive_workspace_summary_view", _spy)
    if hasattr(shell_out_mod, "derive_workspace_summary_view"):
        monkeypatch.setattr(shell_out_mod, "derive_workspace_summary_view", _spy)

    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_run_subprocess_with_envelope(monkeypatch, common_mod, _v1_single_payload("WORKSTATE-REF-77"))

    shell_out_mod._read_active_task_ref(repo)
    assert sentinel_calls, (
        "shell_out._read_active_task_ref did not invoke "
        "handlers._common.derive_workspace_summary_view — the reader "
        "is still on the on-disk-file path. Migrate it through the "
        "derive-on-read helper (WORKSTATE-REF-54-FU implementation note)."
    )
