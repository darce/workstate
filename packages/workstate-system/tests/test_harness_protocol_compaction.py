from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

yaml = pytest.importorskip("yaml")

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PACKAGE_ROOT / "docs" / "agentic" / "contracts" / "harness-protocol.yaml"
sys.path.insert(0, str(PACKAGE_ROOT))

from scripts.check_harness_sync import FIXTURE_PACKAGE_SRC, _fixture_env


def _load_contract() -> dict:
    payload = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8")) or {}
    assert isinstance(payload, dict), "harness-protocol.yaml must parse to a mapping"
    return payload


def test_compaction_contract_block_exists_with_expected_shape() -> None:
    contract = _load_contract()
    compaction = contract.get("compaction")

    assert isinstance(compaction, dict), (
        "WORKSTATE-REF-1 implementation note: harness-protocol.yaml must define a top-level "
        "`compaction:` mapping."
    )
    assert compaction.get("advisory_field") == "compaction_recommended"
    assert isinstance(compaction.get("threshold_tokens"), int)
    assert isinstance(compaction.get("threshold_chars"), int)
    assert compaction.get("unknown_harness") == "warn_and_skip"

    transcript_discovery = compaction.get("transcript_discovery")
    assert isinstance(transcript_discovery, dict)
    assert set(transcript_discovery) == {"claude-code", "codex", "vscode"}

    for harness, rule in transcript_discovery.items():
        assert isinstance(rule, dict), f"{harness} transcript discovery must be a mapping"
        assert isinstance(rule.get("env_var"), str) and rule["env_var"]
        assert isinstance(rule.get("fallback_glob"), str) and rule["fallback_glob"]


def test_fixture_env_hides_live_handoff_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli_dir = tmp_path / "bin"
    cli_dir.mkdir()
    fake_cli = cli_dir / "mcp-workstate-handoff"
    fake_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_cli.chmod(0o755)

    other_dir = tmp_path / "other-bin"
    other_dir.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join((str(cli_dir), str(other_dir))))
    monkeypatch.delenv("PYTHONPATH", raising=False)

    repo = tmp_path / "repo"
    env = _fixture_env(repo)

    assert env["CLAUDE_PROJECT_DIR"] == str(repo)
    assert env["WORKSTATE_SKIP_ACTIVE_TASK_PROBE"] == "1"
    assert str(repo / FIXTURE_PACKAGE_SRC) in env["PYTHONPATH"].split(os.pathsep)[0]
    assert str(cli_dir) in env["PATH"].split(os.pathsep)
    assert str(other_dir) in env["PATH"].split(os.pathsep)