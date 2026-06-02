"""implementation note C1: the lifecycle CLI converts a KeyboardInterrupt (operator
Ctrl-C during a gate's subprocess call) into a clean exit code 130 with a
one-line message, instead of letting a multi-frame traceback escape.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "scripts" / "workstate" / "lifecycle"
if str(LIFECYCLE_PKG) not in sys.path:
    sys.path.insert(0, str(LIFECYCLE_PKG))

import cli  # noqa: WORKSTATE-REF-402


def test_main_converts_keyboard_interrupt_to_clean_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _interrupt(_rest: list[str]) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.context, "run", _interrupt)

    rc = cli.main(["context"])

    assert rc == 130
    err = capsys.readouterr().err
    assert "interrupted" in err.lower(), err
    assert "Traceback" not in err, err
