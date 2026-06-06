from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
README = PACKAGE_ROOT / "README.md"


def _readme() -> str:
    return README.read_text()


def test_readme_uvx_invocation_is_version_pinned() -> None:
    text = _readme()
    assert "uvx mcp-workstate-handoff@" in text, (
        "README should show a version-pinned uvx invocation "
        "(`uvx mcp-workstate-handoff@<version>`) so consumers pin a known release."
    )


def test_readme_client_adapter_section_includes_pinned_uvx_variant() -> None:
    text = _readme()
    assert "Pinned via uvx" in text, (
        "README Client Adapter Shape section should include a 'Pinned via uvx' adapter example block."
    )
    assert '"uvx"' in text, "Pinned uvx adapter example should set `command` to `uvx`."
    assert "mcp-workstate-handoff@" in text, (
        "Pinned uvx adapter args should reference `mcp-workstate-handoff@<version>`."
    )


def test_readme_has_version_pinning_subsection() -> None:
    text = _readme()
    assert "## Version pinning" in text or "### Version pinning" in text, (
        "README should expose a 'Version pinning' subsection that explains "
        "how to verify and pin the running server version."
    )


def test_readme_version_pinning_documents_introspection_paths() -> None:
    text = _readme()
    assert "mcp-workstate-handoff --version" in text, (
        "Version pinning subsection should document the `--version` CLI flag."
    )
    assert '"version"' in text, (
        'Version pinning subsection should reference the doctor `version` top-level field (e.g. `"version": "0.11.0"`).'
    )
