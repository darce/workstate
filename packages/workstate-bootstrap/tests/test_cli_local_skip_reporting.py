"""install/update CLI reporting of local-precedence skips (implementation note implementation note).

When install/update leaves a managed surface untouched under local precedence
(``source=local`` in the receipt), the CLI must never be silent about it: one
line per skipped surface plus an aggregate count, so a receipt that says
``remote_ref=vX.Y.Z`` can no longer masquerade as a fully-updated tree.
"""

from __future__ import annotations

import pytest

from workstate_bootstrap.cli import _print_local_precedence_skips


def _manifest(surfaces: list[dict[str, str]]) -> dict[str, object]:
    return {
        "schema_version": 9,
        "remote_ref": "v0.1.23",
        "remote_sha": "8" * 40,
        "surfaces": surfaces,
    }


def test_no_local_surfaces_prints_nothing(capsys: pytest.CaptureFixture) -> None:
    _print_local_precedence_skips(
        _manifest([{"path": "scripts/hooks", "source": "shared"}])
    )

    assert capsys.readouterr().out == ""


def test_one_line_per_local_precedence_skip_plus_summary(
    capsys: pytest.CaptureFixture,
) -> None:
    _print_local_precedence_skips(
        _manifest(
            [
                {"path": "scripts/hooks", "source": "local"},
                {"path": ".github/hooks", "source": "local"},
                {"path": "docs/workstate", "source": "shared"},
            ]
        )
    )

    out = capsys.readouterr().out
    assert (
        "skipped (local precedence): scripts/hooks — run doctor for drift detail"
        in out
    )
    assert (
        "skipped (local precedence): .github/hooks — run doctor for drift detail"
        in out
    )
    assert "2 surface(s) kept under local precedence" in out


def test_install_and_update_call_sites_emit_skip_lines(
    capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wire-up check: both install branches and update route their manifest
    through the skip reporter."""
    import workstate_bootstrap.cli as cli

    local_manifest = _manifest(
        [{"path": "scripts/hooks", "source": "local"}]
    ) | {"source_kind": "git_overlay"}

    monkeypatch.setattr(cli, "install", lambda **kwargs: dict(local_manifest))
    monkeypatch.setattr(cli, "update", lambda **kwargs: dict(local_manifest))

    rc = cli.main(
        ["install", "--remote-url", "file:///tmp/fake.git", "--target", "/tmp/x"]
    )
    assert rc == 0
    assert "skipped (local precedence): scripts/hooks" in capsys.readouterr().out

    rc = cli.main(["update", "--remote-ref", "v0.1.23", "--target", "/tmp/x"])
    assert rc == 0
    assert "skipped (local precedence): scripts/hooks" in capsys.readouterr().out
