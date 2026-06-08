"""implementation note implementation note: characterization net for ``install()`` manifests.

Golden-manifest tests pin current behavior across source kinds and profiles so
later refactor slices (Split Phase, harness adapters, …) can prove
byte-identical output through S4.

Each scenario runs install in a tmp target, compares the written manifest to a
checked-in golden JSON, and includes a fault-injection companion that proves the
golden assertion bites (mutate one field → test fails).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_install import fake_remote_with_generator  # noqa: F401
from tests.test_install_profiles import fake_remote_with_lifecycle  # noqa: F401
from tests.test_package_source import _build_and_unpack_package

_FIXTURES = Path(__file__).resolve().parent / "golden" / "install_manifests"


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


def _init_git_repo(path: Path) -> None:
    _git("init", "--initial-branch=main", cwd=path)
    _git("config", "user.email", "char@example.com", cwd=path)
    _git("config", "user.name", "Characterization", cwd=path)


def _load_golden(name: str) -> dict[str, Any]:
    path = _FIXTURES / f"{name}.json"
    assert path.is_file(), f"missing golden fixture: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_on_disk(target: Path) -> dict[str, Any]:
    manifest_path = target / ".workstate-bootstrap.json"
    assert manifest_path.is_file(), "install must write .workstate-bootstrap.json"
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _normalize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Strip per-run volatile provenance while preserving structural contract."""
    normalized = json.loads(json.dumps(manifest))
    if "remote_url" in normalized:
        normalized["remote_url"] = "<REMOTE_URL>"
    if "remote_sha" in normalized:
        normalized["remote_sha"] = "<REMOTE_SHA>"
    # Same ambient-venv residue class as stack_* below: the recorded
    # package_version is whatever workstate-system wheel the dev/CI venv
    # carries (0.2.4 vs 0.2.5 re-cut), not a property of the install logic.
    if "package_version" in normalized:
        normalized["package_version"] = "<PACKAGE_VERSION>"
    for volatile in (
        "install_steps",
        "presync_projects",
        "prewarm_refs",
        "offline_latch",
        # Stack provenance reads the ambient installed workstate-stack
        # distribution, so it varies with the dev/CI venv — strip it to keep
        # the golden hermetic.
        "stack_distribution",
        "stack_version",
        "stack_members",
    ):
        normalized.pop(volatile, None)
    surfaces = normalized.get("surfaces")
    if isinstance(surfaces, list):
        for entry in surfaces:
            if isinstance(entry, dict) and "provenance_key" in entry:
                entry["provenance_key"] = "<PROVENANCE>"
    return normalized


def _assert_manifest_matches_golden(
    actual: dict[str, Any], golden: dict[str, Any], *, scenario: str
) -> None:
    assert _normalize_manifest(actual) == _normalize_manifest(golden), (
        f"manifest drift in {scenario}; rerun capture if intentional:\n"
        f"  expected keys: {sorted(golden)}\n"
        f"  actual keys:   {sorted(actual)}"
    )


@pytest.mark.parametrize(
    "profile,fixture_name",
    [
        ("minimal", "git_overlay_minimal"),
        ("lifecycle", "git_overlay_lifecycle"),
        ("all", "git_overlay_all"),
    ],
)
def test_git_overlay_profile_manifest_matches_golden(
    tmp_path: Path,
    fake_remote_with_lifecycle: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    fixture_name: str,
) -> None:
    from workstate_bootstrap.install import install

    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake-home"))
    (tmp_path / "fake-home").mkdir()

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_lifecycle

    install(
        target=target, remote_url=url, remote_ref=ref, profile=profile, mcp_servers=None
    )

    actual = _manifest_on_disk(target)
    golden = _load_golden(fixture_name)
    _assert_manifest_matches_golden(actual, golden, scenario=fixture_name)


def test_git_overlay_all_with_generator_matches_golden(
    tmp_path: Path,
    fake_remote_with_generator: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workstate_bootstrap.install import install

    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake-home"))
    (tmp_path / "fake-home").mkdir()

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_generator

    install(
        target=target,
        remote_url=url,
        remote_ref=ref,
        profile="all",
        mcp_servers=None,
        enforce_required_surfaces=False,
    )

    actual = _manifest_on_disk(target)
    golden = _load_golden("git_overlay_all_full")
    _assert_manifest_matches_golden(actual, golden, scenario="git_overlay_all_full")


def test_package_all_matches_golden(tmp_path: Path) -> None:
    from workstate_bootstrap.install import install

    package_root = _build_and_unpack_package(tmp_path)
    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    install(
        target=target,
        source="package",
        package_root=package_root,
        mcp_servers=None,
        enforce_required_surfaces=False,
        profile="all",
    )

    actual = _manifest_on_disk(target)
    golden = _load_golden("package_all")
    _assert_manifest_matches_golden(actual, golden, scenario="package_all")


@pytest.mark.parametrize(
    "fixture_name",
    [
        # RF29-S1-01 (implementation note implementation note): every checked-in golden must prove
        # its assertion bites, not just the first two scenarios.
        "git_overlay_minimal",
        "git_overlay_lifecycle",
        "git_overlay_all",
        "git_overlay_all_full",
        "package_all",
    ],
)
def test_golden_fixture_bites_on_injected_fault(fixture_name: str) -> None:
    """Prove each golden comparison fails when the expected manifest is wrong."""
    golden = _load_golden(fixture_name)
    mutated = dict(golden)
    mutated["profile"] = "__injected_fault__"
    with pytest.raises(AssertionError, match="manifest drift"):
        _assert_manifest_matches_golden(golden, mutated, scenario=fixture_name)
