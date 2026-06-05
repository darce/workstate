"""Cross-source version-truth pin for `workstate-protocol`.

Three sources can claim the package version: ``pyproject.toml``,
``workstate_protocol.__version__``, and the ``CHANGELOG.md`` release
sections. Downstream packages (``mcp-workstate-handoff``,
``workstate-bootstrap``) pin dependency floors against the pyproject
version. If runtime ``__version__`` or the CHANGELOG drifts away from
that, dependency-floor citations become circular ("we require X.Y.Z
because that's the first release shipping the contract" — but no
release X.Y.Z exists in the CHANGELOG, and the runtime self-reports
something else).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    with (PACKAGE_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def test_runtime_version_matches_pyproject() -> None:
    import workstate_protocol  # local import so test discovery does not crash on missing module

    assert workstate_protocol.__version__ == _pyproject_version(), (
        f"workstate_protocol.__version__ ({workstate_protocol.__version__!r}) must "
        f"match pyproject.toml version ({_pyproject_version()!r}). Downstream "
        f"packages pin dependency floors against the pyproject value; runtime "
        f"drift makes the floor citation a runtime lie."
    )


def test_changelog_has_release_section_for_pyproject_version() -> None:
    version = _pyproject_version()
    changelog = (PACKAGE_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^##\s*\[?{re.escape(version)}\]?\s+—",
        re.MULTILINE,
    )
    # Accept either em-dash (U+2014) or ASCII hyphen as the date separator.
    pattern_ascii = re.compile(
        rf"^##\s*\[?{re.escape(version)}\]?\s+[-—]",
        re.MULTILINE,
    )
    assert pattern_ascii.search(changelog), (
        f"CHANGELOG.md must contain a release section for the published "
        f"version {version!r} (e.g. '## [{version}] — YYYY-MM-DD'). "
        f"Downstream packages cite this release in their dependency-floor "
        f"justifications; missing it makes the citation circular."
    )
