from workstate_orchestrator_mcp.orchestration import handoff_integrity_guard


def test_handoff_integrity_guard_discovers_current_package_directory(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    guard_path = (
        repo_root
        / "packages"
        / "mcp-workstate-orchestrator"
        / "src"
        / "workstate_orchestrator_mcp"
        / "orchestration"
        / "handoff_integrity_guard.py"
    )
    guard_path.parent.mkdir(parents=True)
    guard_path.write_text("# test guard path\n")
    monkeypatch.delenv("ORCHESTRATOR_ROOT", raising=False)
    monkeypatch.setattr(handoff_integrity_guard, "__file__", str(guard_path))

    assert handoff_integrity_guard._discover_repo_root() == repo_root.resolve()


def test_handoff_integrity_guard_rejects_legacy_package_directory(tmp_path):
    legacy_only_root = tmp_path / "legacy-only"
    (legacy_only_root / "packages" / "workstate-orchestrator-mcp").mkdir(parents=True)

    assert handoff_integrity_guard._has_orchestrator_package(legacy_only_root) is False


def test_handoff_integrity_guard_accepts_current_package_directory(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / "packages" / "mcp-workstate-orchestrator").mkdir(parents=True)

    assert handoff_integrity_guard._has_orchestrator_package(repo_root) is True
