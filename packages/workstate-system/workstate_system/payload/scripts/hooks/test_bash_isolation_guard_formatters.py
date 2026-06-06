"""Regression tests for FU-01 formatter detection in _bash_isolation_guard.

Contract: when the Bash command matches a known in-place formatter pattern,
`scan_bash_command` reports every configured `code_roots` entry (and
`root_protected_files`) as blocked with a `<root>/ (formatter)` label, so the
caller (guard-bash-main-branch.py) surfaces the contract-backed violation.
Unrelated commands (read-only linters, `make test-*`, plain `ruff check`)
must NOT trigger the formatter branch.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "hooks"))

from _bash_isolation_guard import scan_bash_command  # noqa: WORKSTATE-REF-402
from _harness_protocol import BranchIsolationPolicy  # noqa: WORKSTATE-REF-402


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


# REVGUARD-F1 made the formatter block branch-aware, so the assertions below
# must scan against a repo whose branch is deterministic — not the dev
# checkout, whose branch depends on where the suite happens to run.
@pytest.fixture(scope="session")
def main_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("fmt-main")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "Makefile").write_text("all:\n\ttrue\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")
    return root


def _policy() -> BranchIsolationPolicy:
    return BranchIsolationPolicy(
        code_roots=("apps/", "packages/", "scripts/", ".github/hooks/", ".claude/", "mk/"),
        protected_extensions=(".py", ".ts", ".tsx", ".js", ".jsx", ".php", ".sql", ".sh", ".css", ".scss", ".mk"),
        root_protected_files=("Makefile",),
        protected_main_surfaces=(),
        permitted_main_surfaces=(),
    )


def _assert_formatter_blocked(main_repo: Path, command: str) -> None:
    blocked = scan_bash_command(command, main_repo, _policy())
    labels = [b for b in blocked if b.endswith("(formatter)")]
    assert labels, f"expected formatter-labelled blocked entries for {command!r}, got {blocked!r}"
    # Every configured code_root surfaces in the output.
    for root in ("apps", "packages", "scripts", ".github/hooks", ".claude", "mk"):
        assert any(root in entry for entry in labels), (
            f"missing root `{root}` in formatter-blocked set for {command!r}: {labels!r}"
        )


def _assert_not_formatter(main_repo: Path, command: str) -> None:
    blocked = scan_bash_command(command, main_repo, _policy())
    labels = [b for b in blocked if b.endswith("(formatter)")]
    assert not labels, f"unexpected formatter flag for {command!r}: {labels!r}"


def test_make_format_all_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "make format-all")


def test_make_format_handoff_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "make format-handoff")


def test_make_fix_lint_handoff_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "make fix-lint-handoff")


def test_make_fix_php_style_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "make fix-php-style")


def test_ruff_format_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "ruff format packages/")


def test_ruff_check_fix_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "ruff check --fix packages/")


def test_ruff_check_readonly_not_blocked(main_repo: Path) -> None:
    _assert_not_formatter(main_repo, "ruff check packages/")


def test_black_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "black packages/")


def test_prettier_write_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "prettier --write apps/")


def test_prettier_short_w_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "prettier -w apps/**/*.ts")


def test_prettier_plain_blocked(main_repo: Path) -> None:
    # Bare `prettier` without --write is write-by-default in some setups; the
    # conservative registry still flags `prettier` because `None` matches verb
    # alone. Guarding bias: prefer false-positive over silent drift.
    _assert_formatter_blocked(main_repo, "prettier apps/")


def test_npm_run_format_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "npm run format")


def test_npm_run_lint_fix_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "npm run lint:fix")


def test_pnpm_run_fix_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "pnpm run fix")


def test_yarn_format_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "yarn format")


def test_composer_run_format_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "composer run format")


def test_composer_fix_style_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "composer fix-style")


def test_eslint_fix_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "eslint --fix apps/")


def test_eslint_readonly_not_blocked(main_repo: Path) -> None:
    _assert_not_formatter(main_repo, "eslint apps/")


def test_stylelint_fix_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "stylelint --fix apps/")


def test_make_test_not_formatter(main_repo: Path) -> None:
    _assert_not_formatter(main_repo, "make test-handoff")


def test_make_check_all_not_formatter(main_repo: Path) -> None:
    _assert_not_formatter(main_repo, "make check-all")


def test_npm_run_test_not_formatter(main_repo: Path) -> None:
    _assert_not_formatter(main_repo, "npm run test")


def test_pytest_not_formatter(main_repo: Path) -> None:
    _assert_not_formatter(main_repo, "pytest packages/mcp-workstate-handoff/tests/")


def test_env_prefix_does_not_confuse_detector(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "FOO=bar make format-all")


def test_sudo_prefix_does_not_confuse_detector(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "sudo ruff format .")


def test_chained_commands_trigger_on_formatter_stage(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "git status && ruff format packages/")


def test_chained_commands_no_trigger_when_no_formatter(main_repo: Path) -> None:
    _assert_not_formatter(main_repo, "git status && ruff check packages/")


# --- read-only invocations of formatter verbs (REVGUARD-F2) ----------------
# `--check` / `--diff` / `--dry-run` runs verify formatting without writing;
# they must not trip the in-place-formatter branch.


def test_ruff_format_check_not_formatter(main_repo: Path) -> None:
    _assert_not_formatter(main_repo, "ruff format --check packages/")


def test_ruff_format_diff_not_formatter(main_repo: Path) -> None:
    _assert_not_formatter(main_repo, "ruff format --diff packages/")


def test_black_check_not_formatter(main_repo: Path) -> None:
    _assert_not_formatter(main_repo, "black --check packages/")


def test_prettier_check_not_formatter(main_repo: Path) -> None:
    _assert_not_formatter(main_repo, "prettier --check apps/")


def test_make_dry_run_format_all_not_formatter(main_repo: Path) -> None:
    _assert_not_formatter(main_repo, "make --dry-run format-all")


def test_readonly_flag_with_explicit_fix_flag_stays_blocked(main_repo: Path) -> None:
    # Conservative bias: a write flag alongside a read-only flag still counts
    # as a formatter run; prefer false-positive over silent drift.
    _assert_formatter_blocked(main_repo, "ruff check --fix --diff packages/")


# --- formatter-stage cwd worktree-branch resolution (REVGUARD-F1) -----------
# A formatter run whose effective cwd is a feature-branch worktree is not a
# main-branch write. Parity with the per-path `resolve_path_branch` rule that
# explicit write targets already get. Unknown cwd stays fail-closed.


@pytest.fixture()
def fmt_repo_pair(tmp_path: Path) -> tuple[Path, Path]:
    """(primary repo on main, linked feature-branch worktree)."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-b", "main")
    _git(primary, "config", "user.email", "t@example.invalid")
    _git(primary, "config", "user.name", "t")
    (primary / "Makefile").write_text("all:\n\ttrue\n")
    pkg = primary / "packages" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "mod.py").write_text("x = 1\n")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-m", "init")
    worktree = tmp_path / "wt"
    _git(primary, "worktree", "add", "-b", "feature/task", str(worktree))
    return primary, worktree


def test_formatter_after_cd_into_feature_worktree_allowed(fmt_repo_pair) -> None:
    primary, wt = fmt_repo_pair
    blocked = scan_bash_command(f"cd {wt} && make format-all", primary, _policy())
    labels = [b for b in blocked if b.endswith("(formatter)")]
    assert not labels, f"formatter in feature-branch worktree must not block: {labels!r}"


def test_formatter_without_cd_on_main_blocked(fmt_repo_pair) -> None:
    primary, _wt = fmt_repo_pair
    blocked = scan_bash_command("make format-all", primary, _policy())
    assert any(b.endswith("(formatter)") for b in blocked)


def test_formatter_after_cd_into_primary_on_main_blocked(fmt_repo_pair) -> None:
    primary, _wt = fmt_repo_pair
    blocked = scan_bash_command(f"cd {primary} && make format-all", primary, _policy())
    assert any(b.endswith("(formatter)") for b in blocked)


def test_formatter_after_unresolvable_cd_fail_closed(fmt_repo_pair) -> None:
    primary, _wt = fmt_repo_pair
    blocked = scan_bash_command('cd "$SOMEWHERE" && make format-all', primary, _policy())
    assert any(b.endswith("(formatter)") for b in blocked)


def test_formatter_after_piped_cd_fail_closed(fmt_repo_pair) -> None:
    # `cd X | make format-all` runs the cd in a pipeline subshell; the cwd
    # does not propagate, so the formatter stage cwd is unknown.
    primary, wt = fmt_repo_pair
    blocked = scan_bash_command(f"cd {wt} | make format-all", primary, _policy())
    assert any(b.endswith("(formatter)") for b in blocked)


def test_mixed_stages_block_when_any_formatter_on_main(fmt_repo_pair) -> None:
    primary, wt = fmt_repo_pair
    blocked = scan_bash_command(
        f"cd {wt} && make format-all && cd {primary} && ruff format .",
        primary,
        _policy(),
    )
    assert any(b.endswith("(formatter)") for b in blocked)


def test_formatter_cwd_outside_any_repo_fail_closed(fmt_repo_pair, tmp_path: Path) -> None:
    primary, _wt = fmt_repo_pair
    outside = tmp_path / "no-repo"
    outside.mkdir()
    blocked = scan_bash_command(f"cd {outside} && make format-all", primary, _policy())
    assert any(b.endswith("(formatter)") for b in blocked)


def test_formatter_cd_semicolon_joiner_propagates(fmt_repo_pair) -> None:
    primary, wt = fmt_repo_pair
    blocked = scan_bash_command(f"cd {wt} ; make format-all", primary, _policy())
    labels = [b for b in blocked if b.endswith("(formatter)")]
    assert not labels, f"`;`-joined cd must propagate like `&&`: {labels!r}"


def test_formatter_on_master_branch_blocked(tmp_path: Path) -> None:
    repo = tmp_path / "master-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "Makefile").write_text("all:\n\ttrue\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    blocked = scan_bash_command("make format-all", repo, _policy())
    assert any(b.endswith("(formatter)") for b in blocked)


def test_readonly_and_write_flag_combo_stays_blocked(main_repo: Path) -> None:
    _assert_formatter_blocked(main_repo, "prettier --write --check apps/")


# --- formatter explicit path args (REVGUARD cycle-1 A-01) -------------------
# Direct formatter verbs accept path arguments; a path that resolves into a
# protected-branch checkout must block even when the stage cwd is a
# feature-branch worktree.


def test_formatter_absolute_path_arg_into_main_blocked(fmt_repo_pair) -> None:
    primary, wt = fmt_repo_pair
    blocked = scan_bash_command(f"cd {wt} && ruff format {primary}/packages/", primary, _policy())
    assert blocked, "absolute formatter path arg into main checkout must block"


def test_formatter_relative_escape_path_arg_into_main_blocked(fmt_repo_pair) -> None:
    primary, wt = fmt_repo_pair
    blocked = scan_bash_command(
        "cd " + str(wt) + " && black ../primary/packages/pkg/mod.py", primary, _policy()
    )
    assert blocked, "relative-escape formatter path arg into main checkout must block"


def test_formatter_relative_path_arg_inside_feature_worktree_allowed(fmt_repo_pair) -> None:
    primary, wt = fmt_repo_pair
    blocked = scan_bash_command(f"cd {wt} && ruff format packages/", primary, _policy())
    assert blocked == [], f"feature-worktree-local formatter paths must not block: {blocked!r}"


# --- make -C redirection (REVGUARD cycle-1 A-02) ----------------------------


def test_make_dash_c_into_main_checkout_blocked(fmt_repo_pair) -> None:
    primary, wt = fmt_repo_pair
    blocked = scan_bash_command(f"cd {wt} && make -C {primary} format-all", primary, _policy())
    assert any(b.endswith("(formatter)") for b in blocked)


def test_make_dash_c_into_feature_worktree_allowed(fmt_repo_pair) -> None:
    primary, wt = fmt_repo_pair
    blocked = scan_bash_command(f"make -C {wt} format-all", primary, _policy())
    labels = [b for b in blocked if b.endswith("(formatter)")]
    assert not labels, f"make -C into feature worktree must not block: {labels!r}"


def test_make_dash_c_unresolvable_fail_closed(fmt_repo_pair) -> None:
    primary, wt = fmt_repo_pair
    blocked = scan_bash_command(f'cd {wt} && make -C "$DIR" format-all', primary, _policy())
    assert any(b.endswith("(formatter)") for b in blocked)


def test_make_directory_flag_into_main_checkout_blocked(fmt_repo_pair) -> None:
    primary, wt = fmt_repo_pair
    blocked = scan_bash_command(
        f"cd {wt} && make --directory={primary} format-all", primary, _policy()
    )
    assert any(b.endswith("(formatter)") for b in blocked)
