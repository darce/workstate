"""WORKSTATE-REF-07 implementation note: operator surface — doctor finding kinds, overrides status,
accept-upstream, and the repair mutation guard."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from tests.test_install import _git, fake_remote_with_generator  # noqa: F401


def _system_payload_root() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "workstate-system"
        / "workstate_system"
        / "payload"
    )


def _canonical_skill(slug: str) -> str:
    payload_root = _system_payload_root()
    structured = yaml.safe_load(
        (payload_root / "skills" / slug / "skill.yaml").read_text()
    )
    structured.pop("generator", None)
    body = (payload_root / "skills" / slug / "body.md").read_text()
    fm_text = yaml.safe_dump(
        structured, sort_keys=False, default_flow_style=False
    ).rstrip()
    body = body if body.endswith("\n") else body + "\n"
    return f"---\n{fm_text}\n---\n\n{body}"


def _sha256(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _seed_patch_override(
    target: Path, *, conflicting: bool = False
) -> tuple[Path, str]:
    """Seed a patch-mode branch-review override. Returns (override_root,
    forked_base). With ``conflicting=True`` the consumer edit rewrites the
    same final line the forked base diverges on, guaranteeing a merge
    conflict against current upstream."""
    canonical = _canonical_skill("branch-review")
    lines = canonical.splitlines(keepends=True)
    forked_base = "".join(lines[:-1]) + "OLD ENDING\n"
    if conflicting:
        consumer_edit = "".join(lines[:-1]) + "CONSUMER ENDING\n"
    else:
        consumer_edit = (
            lines[0] + "<!-- consumer customization -->\n" + "".join(lines[1:-1]) + "OLD ENDING\n"
        )

    override_root = target / "workstate-overrides" / "workstate-system"
    skill_dir = override_root / "skills" / "branch-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.base.md").write_text(forked_base)
    (skill_dir / "SKILL.md").write_text(consumer_edit)
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "skills": {
                        "branch-review": {
                            "mode": "patch",
                            "path": "skills/branch-review/SKILL.md",
                            "base_path": "skills/branch-review/SKILL.base.md",
                            "upstream_digest": _sha256(forked_base),
                            "on_upstream_change": "warn",
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )
    return override_root, forked_base


def _installed_consumer(
    tmp_path: Path, fake_remote: tuple[str, str], *, conflicting: bool = False
) -> tuple[Path, Path]:
    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _git("init", "--initial-branch=main", cwd=target)
    _git("config", "user.email", "test@example.com", cwd=target)
    _git("config", "user.name", "Test", cwd=target)

    override_root, _ = _seed_patch_override(target, conflicting=conflicting)

    url, ref = fake_remote
    install(target=target, remote_url=url, remote_ref=ref)
    return target, override_root


def test_doctor_reports_override_merge_conflict(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.subcommands import doctor

    target, _ = _installed_consumer(
        tmp_path, fake_remote_with_generator, conflicting=True
    )

    kinds = {finding["kind"] for finding in doctor(target=target)}
    assert "override_merge_conflict" in kinds


def test_doctor_reports_effective_tree_missing(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    import shutil

    from workstate_bootstrap.subcommands import doctor

    from workstate_bootstrap.install import install

    target = tmp_path / "consumer"
    target.mkdir()
    _git("init", "--initial-branch=main", cwd=target)
    _git("config", "user.email", "test@example.com", cwd=target)
    _git("config", "user.name", "Test", cwd=target)

    url, ref = fake_remote_with_generator
    install(target=target, remote_url=url, remote_ref=ref)

    shutil.rmtree(
        target / ".workstate" / "generated" / "plugins" / "workstate-system" / "effective"
    )

    findings = doctor(target=target)
    missing = [f for f in findings if f["kind"] == "effective_tree_missing"]
    assert missing, findings
    assert "install" in missing[0]["message"] or "update" in missing[0]["message"]
    # The whole-tree-missing diagnosis subsumes per-pin source drift noise.
    assert not any(f["kind"] == "plugin_source_drift" for f in findings)


def test_overrides_status_reports_conflict_and_clean(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.overrides import overrides_status

    target, _ = _installed_consumer(
        tmp_path, fake_remote_with_generator, conflicting=True
    )

    report = overrides_status(target=target)
    rows = {row["name"]: row for row in report["components"]}
    assert rows["branch-review"]["mode"] == "patch"
    assert rows["branch-review"]["status"] == "merge_conflict"


def test_accept_upstream_repins_digest_refreshes_base_and_records_provenance(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.overrides import accept_upstream

    target, override_root = _installed_consumer(
        tmp_path, fake_remote_with_generator, conflicting=False
    )
    _git("add", "-A", cwd=target)
    _git("commit", "-m", "baseline", cwd=target)

    base_skill_path = (
        target
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "base"
        / "claude"
        / "skills"
        / "branch-review"
        / "SKILL.md"
    )
    current_upstream = base_skill_path.read_text()

    old_manifest = yaml.safe_load((override_root / "overrides.yaml").read_text())
    old_digest = old_manifest["components"]["skills"]["branch-review"][
        "upstream_digest"
    ]
    assert old_digest != _sha256(current_upstream)

    receipt = accept_upstream(target=target, skill="branch-review")
    assert receipt["skill"] == "branch-review"
    assert receipt["previous_upstream_digest"] == old_digest
    assert receipt["new_upstream_digest"] == _sha256(current_upstream)

    manifest = yaml.safe_load((override_root / "overrides.yaml").read_text())
    assert (
        manifest["components"]["skills"]["branch-review"]["upstream_digest"]
        == _sha256(current_upstream)
    )
    # patch mode: the stored base copy is refreshed to current upstream.
    assert (
        override_root / "skills" / "branch-review" / "SKILL.base.md"
    ).read_text() == current_upstream

    lock = json.loads((override_root / "overrides.lock.json").read_text())
    entry = next(
        item for item in lock["components"] if item["name"] == "branch-review"
    )
    assert entry["last_accept_upstream"]["previous_upstream_digest"] == old_digest
    assert entry["last_accept_upstream"]["new_upstream_digest"] == _sha256(
        current_upstream
    )
    assert entry["last_accept_upstream"]["accepted_at"]


def test_accept_upstream_refuses_dirty_override_root_unless_force(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.overrides import DirtyOverrideRootError, accept_upstream

    target, override_root = _installed_consumer(
        tmp_path, fake_remote_with_generator, conflicting=False
    )
    # Override root left uncommitted -> dirty by definition.
    with pytest.raises(DirtyOverrideRootError):
        accept_upstream(target=target, skill="branch-review")

    receipt = accept_upstream(target=target, skill="branch-review", force=True)
    assert receipt["forced"] is True


def test_accept_upstream_rejects_escaping_base_path(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.overrides import OverridesError, accept_upstream

    target, override_root = _installed_consumer(
        tmp_path, fake_remote_with_generator, conflicting=False
    )
    _git("add", "-A", cwd=target)
    _git("commit", "-m", "baseline", cwd=target)

    manifest_path = override_root / "overrides.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["components"]["skills"]["branch-review"]["base_path"] = "../outside.md"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))

    with pytest.raises(OverridesError, match="override paths must be relative"):
        accept_upstream(target=target, skill="branch-review")


def test_repair_never_mutates_override_root(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.subcommands import repair

    target, override_root = _installed_consumer(
        tmp_path, fake_remote_with_generator, conflicting=True
    )

    def _snapshot(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    before = _snapshot(override_root)
    repair(target=target)
    after = _snapshot(override_root)
    assert after == before, "repair must never mutate workstate-overrides/"


def test_cli_overrides_status_and_accept_upstream(
    tmp_path: Path, fake_remote_with_generator: tuple[str, str]
) -> None:
    from workstate_bootstrap.cli import main

    target, override_root = _installed_consumer(
        tmp_path, fake_remote_with_generator, conflicting=False
    )
    _git("add", "-A", cwd=target)
    _git("commit", "-m", "baseline", cwd=target)

    rc = main(["overrides", "status", "--target", str(target), "--json"])
    assert rc == 0

    rc = main(
        ["overrides", "accept-upstream", "--target", str(target), "branch-review"]
    )
    assert rc == 0

    manifest = yaml.safe_load((override_root / "overrides.yaml").read_text())
    base_skill = (
        target
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "base"
        / "claude"
        / "skills"
        / "branch-review"
        / "SKILL.md"
    ).read_text()
    assert (
        manifest["components"]["skills"]["branch-review"]["upstream_digest"]
        == _sha256(base_skill)
    )
