"""implementation note (WORKSTATE-REF-40) Sub-implementation note.5: install profile contract.

Covers two of the seven profiles introduced by implementation note:

* ``minimal`` — does not touch consumer-owned shared/per-agent surfaces,
  does not run the workflow generator, and does not write
  ``Makefile``/``.mcp.json``/``.vscode/mcp.json``/``.codex/config.toml``.
  The manifest is still written.
* ``lifecycle`` — hoists ``Makefile.d/lifecycle.mk`` and
  ``scripts/workstate/lifecycle/`` into the consumer, and idempotently
  injects ``-include Makefile.d/*.mk`` into ``<target>/Makefile`` inside
  a sentinel-bracketed block.

The remaining profiles (skills/hooks/claude/codex/all opt-ins) are
tracked by follow-on commits.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=30,
    )
    return result.stdout.strip()


@pytest.fixture()
def fake_remote_with_lifecycle(tmp_path: Path) -> tuple[str, str]:
    """Local bare git remote that ships the implementation note lifecycle source paths
    under the canonical ``packages/workstate-system/`` layout.
    """
    src = tmp_path / "lc-src"
    src.mkdir()
    _git("init", "--initial-branch=main", cwd=src)
    _git("config", "user.email", "test@example.com", cwd=src)
    _git("config", "user.name", "Test", cwd=src)

    system_subdir = src / "packages" / "workstate-system"
    (system_subdir / "Makefile.d").mkdir(parents=True)
    (system_subdir / "Makefile.d" / "lifecycle.mk").write_text(
        "# fake lifecycle.mk\n.PHONY: context\ncontext:\n\t@echo context\n"
    )
    runner = system_subdir / "scripts" / "workstate" / "lifecycle"
    runner.mkdir(parents=True)
    (runner / "__init__.py").write_text('"""fake lifecycle pkg"""\n')
    (runner / "__main__.py").write_text("import sys\nsys.exit(0)\n")
    (runner / "cli.py").write_text("def main(argv):\n    return 0\n")

    _git("add", "-A", cwd=src)
    _git("commit", "-m", "seed lifecycle", cwd=src)
    _git("tag", "v0.1.0", cwd=src)

    bare = tmp_path / "lc.git"
    _git("clone", "--bare", str(src), str(bare), cwd=tmp_path)
    return f"file://{bare}", "v0.1.0"


def test_install_rejects_unknown_profile(
    tmp_path: Path, fake_remote_with_lifecycle: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_lifecycle

    with pytest.raises(ValueError, match="not a recognized install profile"):
        install(target=target, remote_url=url, remote_ref=ref, profile="frobnicate")


def test_minimal_profile_does_not_materialize_harness_surfaces(
    tmp_path: Path,
    fake_remote_with_lifecycle: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``minimal`` must not write per-agent surfaces, not invoke the
    generator, and not create lifecycle hoist artifacts."""
    from workstate_bootstrap.install import install

    # Sandbox HOME so the harness Stop-hook write (implementation note implementation note)
    # never touches the real ~/.claude/settings.json.
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake-home"))
    (tmp_path / "fake-home").mkdir()

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_lifecycle

    manifest = install(
        target=target, remote_url=url, remote_ref=ref, profile="minimal"
    )

    # Lifecycle hoist artifacts MUST NOT appear under minimal.
    assert not (target / "Makefile").exists()
    assert not (target / "Makefile.d").exists()
    assert not (target / "scripts" / "workstate" / "lifecycle").exists()
    # Per-agent generated surfaces MUST NOT be created under minimal.
    assert not (target / ".claude" / "skills").exists()
    assert not (target / ".claude" / "commands").exists()
    assert not (target / ".github" / "prompts").exists()
    assert not (target / ".codex" / "skills").exists()
    # Shared overlay surfaces MUST NOT be materialized under minimal.
    assert not (target / "scripts" / "hooks").exists()

    surfaces = {entry["path"] for entry in manifest["surfaces"]}
    configs = {entry["path"] for entry in manifest["configs"]}
    assert surfaces == set()
    # WORKSTATE-REF-56 implementation note: the harness Stop hook is opt-in via the manifest
    # walker; under minimal with no opt-in flags, the walker writes
    # nothing. core.hooksPath stays out (target is not a git repo so
    # the silent skip applies).
    assert configs == set()


def test_minimal_profile_preserves_user_owned_makefile_byte_for_byte(
    tmp_path: Path, fake_remote_with_lifecycle: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    user_makefile = target / "Makefile"
    original = "# user Makefile\n.PHONY: hello\nhello:\n\t@echo hi\n"
    user_makefile.write_text(original)
    url, ref = fake_remote_with_lifecycle

    install(target=target, remote_url=url, remote_ref=ref, profile="minimal")

    assert user_makefile.read_text() == original


def test_lifecycle_profile_hoists_make_runner_and_injects_include(
    tmp_path: Path, fake_remote_with_lifecycle: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install
    from workstate_bootstrap.install import (
        LIFECYCLE_INCLUDE_DIRECTIVE,
        LIFECYCLE_INCLUDE_SENTINEL_BEGIN,
        LIFECYCLE_INCLUDE_SENTINEL_END,
    )

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_lifecycle

    manifest = install(
        target=target, remote_url=url, remote_ref=ref, profile="lifecycle"
    )

    fragment = target / "Makefile.d" / "lifecycle.mk"
    runner_pkg = target / "scripts" / "workstate" / "lifecycle"
    assert fragment.is_file()
    assert (runner_pkg / "__init__.py").is_file()
    assert (runner_pkg / "__main__.py").is_file()
    assert (runner_pkg / "cli.py").is_file()

    makefile_text = (target / "Makefile").read_text()
    assert LIFECYCLE_INCLUDE_SENTINEL_BEGIN in makefile_text
    assert LIFECYCLE_INCLUDE_DIRECTIVE in makefile_text
    assert LIFECYCLE_INCLUDE_SENTINEL_END in makefile_text

    surfaces = {entry["path"]: entry["source"] for entry in manifest["surfaces"]}
    assert surfaces == {
        "Makefile.d/lifecycle.mk": "lifecycle",
        "scripts/workstate/lifecycle": "lifecycle",
    }
    configs = {entry["path"]: entry["action"] for entry in manifest["configs"]}
    assert configs.get("Makefile") == "created"


def test_lifecycle_profile_is_idempotent_on_rerun(
    tmp_path: Path, fake_remote_with_lifecycle: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install
    from workstate_bootstrap.install import LIFECYCLE_INCLUDE_SENTINEL_BEGIN

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_lifecycle

    install(target=target, remote_url=url, remote_ref=ref, profile="lifecycle")
    first_makefile = (target / "Makefile").read_text()

    manifest = install(
        target=target, remote_url=url, remote_ref=ref, profile="lifecycle"
    )

    second_makefile = (target / "Makefile").read_text()
    assert first_makefile == second_makefile, (
        "second install must not duplicate the sentinel-bracketed include block"
    )
    assert second_makefile.count(LIFECYCLE_INCLUDE_SENTINEL_BEGIN) == 1
    configs = {entry["path"]: entry["action"] for entry in manifest["configs"]}
    assert configs.get("Makefile") == "already_present"


def test_lifecycle_profile_appends_to_existing_user_makefile(
    tmp_path: Path, fake_remote_with_lifecycle: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install
    from workstate_bootstrap.install import (
        LIFECYCLE_INCLUDE_SENTINEL_BEGIN,
    )

    target = tmp_path / "consumer"
    target.mkdir()
    user_block = "# user Makefile\n.PHONY: hello\nhello:\n\t@echo hi\n"
    (target / "Makefile").write_text(user_block)
    url, ref = fake_remote_with_lifecycle

    manifest = install(
        target=target, remote_url=url, remote_ref=ref, profile="lifecycle"
    )

    text = (target / "Makefile").read_text()
    assert text.startswith(user_block), "user-owned content must remain at the top"
    assert LIFECYCLE_INCLUDE_SENTINEL_BEGIN in text
    configs = {entry["path"]: entry["action"] for entry in manifest["configs"]}
    assert configs.get("Makefile") == "appended"


def test_lifecycle_profile_skips_existing_lifecycle_targets(
    tmp_path: Path, fake_remote_with_lifecycle: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install
    from workstate_bootstrap.install import (
        LIFECYCLE_INCLUDE_SENTINEL_BEGIN,
    )

    target = tmp_path / "consumer"
    target.mkdir()
    user_block = "# user Makefile\n.PHONY: context\ncontext:\n\t@./mk/context.sh\n"
    (target / "Makefile").write_text(user_block)
    url, ref = fake_remote_with_lifecycle

    manifest = install(
        target=target, remote_url=url, remote_ref=ref, profile="lifecycle"
    )

    text = (target / "Makefile").read_text()
    assert text == user_block
    assert LIFECYCLE_INCLUDE_SENTINEL_BEGIN not in text
    configs = {entry["path"]: entry["action"] for entry in manifest["configs"]}
    assert configs.get("Makefile") == "skipped_existing_lifecycle_targets"


def test_install_manifest_is_still_written_under_minimal(
    tmp_path: Path, fake_remote_with_lifecycle: tuple[str, str]
) -> None:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_lifecycle

    install(target=target, remote_url=url, remote_ref=ref, profile="minimal")

    manifest_path = target / ".workstate-bootstrap.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["remote_url"] == url
    assert manifest["remote_ref"] == ref
