"""implementation note S3: harness adapter registry contract tests."""

from __future__ import annotations

import re
from pathlib import Path

from workstate_bootstrap.harnesses import HARNESSES

_BOOTSTRAP_SRC = Path(__file__).resolve().parents[1] / "src" / "workstate_bootstrap"

# Conditional dispatch on harness keys must live in harnesses.py only.
_BRANCHING_PATTERNS = (
    re.compile(r"if\s+harness\s*=="),
    re.compile(r"elif\s+harness\s*=="),
    re.compile(r"if\s+harness\s+in\s+"),
    re.compile(r"kind_by_harness\s*="),
    re.compile(r"path_by_harness\s*="),
)


def test_harness_registry_has_four_adapters() -> None:
    assert set(HARNESSES) == {"claude-code", "codex", "vscode", "grok"}


def test_no_harness_conditional_branching_outside_registry() -> None:
    offenders: list[str] = []
    excluded = {
        "harnesses.py",
        # implementation note: doctor/repair read-path; deferred to S6 receipt edges.
        "subcommands.py",
    }
    for path in _BOOTSTRAP_SRC.glob("*.py"):
        if path.name in excluded:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in _BRANCHING_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{path.name}:{pattern.pattern}")
                break
    assert offenders == [], f"harness branching outside registry: {offenders}"


def test_activation_does_not_import_install_privates() -> None:
    activation = (_BOOTSTRAP_SRC / "activation.py").read_text(encoding="utf-8")
    assert "workstate_bootstrap.install import" not in activation