"""implementation note implementation note: Makefile include-marker dedupe + legacy migration.

The 2026-06-05 consumer incident: the consumer Makefile carried the historical
``# >>> WORKSTATE_LIFECYCLE_INCLUDE >>>`` sentinel variant, which
``_ensure_consumer_makefile_include`` did not recognize (it only knew the
current ``WORKSTATE_BOOTSTRAP LIFECYCLE INCLUDE`` and legacy
``AGENTIC_BOOTSTRAP LIFECYCLE INCLUDE`` forms), so install appended a second
block carrying a duplicate ``-include Makefile.d/*.mk`` directive. New
contract:

* a bare ``-include Makefile.d/*.mk`` directive anywhere outside a recipe is
  ``already_present`` regardless of sentinel — never a second append;
* recognized historical sentinel blocks are migrated in place to the current
  sentinel form (one block, one directive, after any install/update);
* the no-Makefile / fresh-append / lifecycle-target-skip behaviors are
  unchanged.
"""

from __future__ import annotations

from pathlib import Path

from workstate_bootstrap.install import (
    LIFECYCLE_INCLUDE_DIRECTIVE,
    LIFECYCLE_INCLUDE_SENTINEL_BEGIN,
    LIFECYCLE_INCLUDE_SENTINEL_END,
    _ensure_consumer_makefile_include,
)


CURRENT_BLOCK = (
    f"{LIFECYCLE_INCLUDE_SENTINEL_BEGIN}\n"
    f"{LIFECYCLE_INCLUDE_DIRECTIVE}\n"
    f"{LIFECYCLE_INCLUDE_SENTINEL_END}\n"
)
AGENTIC_BLOCK = (
    "# >>> AGENTIC_BOOTSTRAP LIFECYCLE INCLUDE >>>\n"
    "-include Makefile.d/*.mk\n"
    "# <<< AGENTIC_BOOTSTRAP LIFECYCLE INCLUDE <<<\n"
)
WORKSTATE_LIFECYCLE_BLOCK = (
    "# >>> WORKSTATE_LIFECYCLE_INCLUDE >>>\n"
    "# workstate lifecycle fragments (managed block)\n"
    "-include Makefile.d/*.mk\n"
    "# <<< WORKSTATE_LIFECYCLE_INCLUDE <<<\n"
)


def _directive_count(text: str) -> int:
    return sum(
        1
        for line in text.splitlines()
        if line.strip() == LIFECYCLE_INCLUDE_DIRECTIVE
    )


def test_current_sentinel_block_is_left_untouched(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text("build:\n\techo hi\n" + CURRENT_BLOCK)

    entry = _ensure_consumer_makefile_include(tmp_path)

    assert entry == {"path": "Makefile", "action": "already_present"}
    assert _directive_count(makefile.read_text()) == 1


def test_workstate_lifecycle_sentinel_variant_is_not_duplicated(
    tmp_path: Path,
) -> None:
    """The exact 2026-06-05 incident shape: an unrecognized historical
    sentinel already carries the directive — install must not append a
    second block."""
    makefile = tmp_path / "Makefile"
    makefile.write_text("build:\n\techo hi\n\n" + WORKSTATE_LIFECYCLE_BLOCK)

    entry = _ensure_consumer_makefile_include(tmp_path)

    text = makefile.read_text()
    assert _directive_count(text) == 1, text
    assert entry is not None and entry["action"] in {
        "already_present",
        "migrated",
    }


def test_recognized_legacy_blocks_migrate_to_current_sentinel(
    tmp_path: Path,
) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text("build:\n\techo hi\n\n" + WORKSTATE_LIFECYCLE_BLOCK)

    entry = _ensure_consumer_makefile_include(tmp_path)

    text = makefile.read_text()
    assert entry == {"path": "Makefile", "action": "migrated"}
    assert LIFECYCLE_INCLUDE_SENTINEL_BEGIN in text
    assert "WORKSTATE_LIFECYCLE_INCLUDE" not in text.replace(
        LIFECYCLE_INCLUDE_SENTINEL_BEGIN, ""
    ).replace(LIFECYCLE_INCLUDE_SENTINEL_END, "")
    assert _directive_count(text) == 1
    assert text.count(LIFECYCLE_INCLUDE_SENTINEL_BEGIN) == 1
    # Consumer content around the block is preserved.
    assert "build:\n\techo hi\n" in text


def test_agentic_legacy_block_migrates_in_place(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text("x := 1\n" + AGENTIC_BLOCK + "y := 2\n")

    entry = _ensure_consumer_makefile_include(tmp_path)

    text = makefile.read_text()
    assert entry == {"path": "Makefile", "action": "migrated"}
    assert "AGENTIC_BOOTSTRAP" not in text
    assert text.count(LIFECYCLE_INCLUDE_SENTINEL_BEGIN) == 1
    assert _directive_count(text) == 1
    assert "x := 1\n" in text and "y := 2\n" in text


def test_bare_directive_without_any_sentinel_is_already_present(
    tmp_path: Path,
) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text(f"build:\n\techo hi\n\n{LIFECYCLE_INCLUDE_DIRECTIVE}\n")

    entry = _ensure_consumer_makefile_include(tmp_path)

    assert entry == {"path": "Makefile", "action": "already_present"}
    assert _directive_count(makefile.read_text()) == 1


def test_directive_inside_recipe_does_not_count_as_present(tmp_path: Path) -> None:
    """A tab-indented recipe line echoing the directive is not a live include."""
    makefile = tmp_path / "Makefile"
    makefile.write_text(f'show:\n\techo "{LIFECYCLE_INCLUDE_DIRECTIVE}"\n')

    entry = _ensure_consumer_makefile_include(tmp_path)

    assert entry == {"path": "Makefile", "action": "appended"}
    text = makefile.read_text()
    assert text.count(LIFECYCLE_INCLUDE_SENTINEL_BEGIN) == 1


def test_fresh_makefile_and_append_paths_unchanged(tmp_path: Path) -> None:
    entry = _ensure_consumer_makefile_include(tmp_path)
    assert entry == {"path": "Makefile", "action": "created"}

    other = tmp_path / "other"
    other.mkdir()
    (other / "Makefile").write_text("build:\n\techo hi\n")
    entry = _ensure_consumer_makefile_include(other)
    assert entry == {"path": "Makefile", "action": "appended"}
    assert (other / "Makefile").read_text().count(LIFECYCLE_INCLUDE_SENTINEL_BEGIN) == 1


def test_migration_is_idempotent(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text(WORKSTATE_LIFECYCLE_BLOCK)

    _ensure_consumer_makefile_include(tmp_path)
    first = makefile.read_text()
    entry = _ensure_consumer_makefile_include(tmp_path)

    assert entry == {"path": "Makefile", "action": "already_present"}
    assert makefile.read_text() == first
