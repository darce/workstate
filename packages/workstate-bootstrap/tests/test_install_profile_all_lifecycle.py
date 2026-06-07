"""WORKSTATE-REF-48: ``--profile all`` must imply the lifecycle hoist.

Drift class fixed by this task: a consumer bootstrapped with
``--profile all`` (the legacy default that ships skills) ended up with
the ``branch-lifecycle`` / ``tdd`` / ``incremental-implementation`` /
``branch-review`` / ``handoff-lifecycle`` skills referencing
``make task-start`` / ``make slice-start`` / ``make context`` /
``make review-ready`` / ``make handoff-close-check`` / ``make format-all``
while the matching ``Makefile.d/lifecycle.mk`` and
``scripts/workstate/lifecycle/`` runner were never installed. ``--profile
all`` and ``--profile lifecycle`` were disjoint branches in
``install()`` and only the latter ran the hoist.

These tests pin the post-fix behavior: ``--profile all`` now performs
the same hoist that ``--profile lifecycle`` performs, so consumers that
ship the lifecycle-referencing skills also receive the runner that
defines those targets.

The fixtures are imported from ``test_install.py`` (the established
fixture home in this package) per the same pattern used by
``test_subcommands.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_install import (  # noqa: F401
    fake_remote_with_generator,
)


def test_profile_all_hoists_lifecycle_runner_and_makefile_include(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-WORKSTATE-REF-48: ``install(profile='all', ...)`` materializes
    ``Makefile.d/lifecycle.mk``, ``scripts/workstate/lifecycle/``, and the
    sentinel-bracketed ``-include`` directive, identical to what the
    dedicated ``lifecycle`` profile produces."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.install import (
        LIFECYCLE_INCLUDE_DIRECTIVE,
        LIFECYCLE_INCLUDE_SENTINEL_BEGIN,
        LIFECYCLE_INCLUDE_SENTINEL_END,
    )

    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake-home"))
    (tmp_path / "fake-home").mkdir()

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator

    install(target=target, remote_url=url, remote_ref=ref, profile="all")

    fragment = target / "Makefile.d" / "lifecycle.mk"
    runner_pkg = target / "scripts" / "workstate" / "lifecycle"
    assert fragment.is_file(), (
        "Makefile.d/lifecycle.mk must be hoisted under --profile all "
        "so lifecycle make targets resolve in the consumer"
    )
    assert (runner_pkg / "__init__.py").is_file()
    assert (runner_pkg / "cli.py").is_file()

    makefile_text = (target / "Makefile").read_text()
    assert LIFECYCLE_INCLUDE_SENTINEL_BEGIN in makefile_text
    assert LIFECYCLE_INCLUDE_DIRECTIVE in makefile_text
    assert LIFECYCLE_INCLUDE_SENTINEL_END in makefile_text


def test_profile_all_manifest_includes_lifecycle_surfaces_and_makefile_include(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The returned manifest under ``--profile all`` must list the two
    lifecycle ``source: 'lifecycle'`` surfaces and (on first run) the
    ``Makefile`` config entry."""
    from workstate_bootstrap.install import install

    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake-home"))
    (tmp_path / "fake-home").mkdir()

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator

    manifest = install(
        target=target, remote_url=url, remote_ref=ref, profile="all"
    )

    lifecycle_surfaces = {
        entry["path"]
        for entry in manifest["surfaces"]
        if entry.get("source") == "lifecycle"
    }
    assert lifecycle_surfaces == {
        "Makefile.d/lifecycle.mk",
        "scripts/workstate/lifecycle",
        # implementation note: the one-shot update surface rides the lifecycle hoist.
        "Makefile.d/update.mk",
        "scripts/workstate/update.sh",
    }

    configs_by_path = {entry["path"]: entry["action"] for entry in manifest["configs"]}
    assert configs_by_path.get("Makefile") == "created"


def test_profile_all_lifecycle_hoist_is_idempotent_on_rerun(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running ``install(profile='all', ...)`` against the same
    target must not duplicate the sentinel-bracketed include block and
    must not raise."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.install import LIFECYCLE_INCLUDE_SENTINEL_BEGIN

    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake-home"))
    (tmp_path / "fake-home").mkdir()

    target = tmp_path / "consumer"
    target.mkdir()
    url, ref = fake_remote_with_generator

    install(target=target, remote_url=url, remote_ref=ref, profile="all")
    first_makefile = (target / "Makefile").read_text()

    manifest = install(
        target=target, remote_url=url, remote_ref=ref, profile="all"
    )

    second_makefile = (target / "Makefile").read_text()
    assert second_makefile.count(LIFECYCLE_INCLUDE_SENTINEL_BEGIN) == 1, (
        "second --profile all install must not duplicate the sentinel "
        "block; _ensure_consumer_makefile_include short-circuits when "
        "the sentinel is already present"
    )
    assert first_makefile == second_makefile

    configs_by_path = {entry["path"]: entry["action"] for entry in manifest["configs"]}
    assert configs_by_path.get("Makefile") == "already_present"
