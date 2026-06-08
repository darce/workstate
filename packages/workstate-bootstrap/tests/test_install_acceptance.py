"""implementation note acceptance gates for deferred open findings."""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest


def _bootstrap_src() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "workstate_bootstrap"


def _executable_body_lines(func_obj: object) -> int:
    source = inspect.getsource(func_obj)
    tree = ast.parse(source)
    func = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    body_start = func.body[0].lineno
    if (
        func.body
        and isinstance(func.body[0], ast.Expr)
        and isinstance(func.body[0].value, ast.Constant)
        and isinstance(func.body[0].value.value, str)
    ):
        body_start = func.body[1].lineno if len(func.body) > 1 else func.end_lineno + 1
    executable = [
        line
        for line in source.splitlines()[body_start - 1 : func.end_lineno]
        if line.strip() and not line.strip().startswith("#")
    ]
    return len(executable)


def test_install_orchestrator_body_within_acceptance_budget() -> None:
    install_mod = importlib.import_module("workstate_bootstrap.install")

    count = _executable_body_lines(install_mod.install)
    assert count <= 40, (
        f"install() orchestrator has {count} executable lines; acceptance budget is 40"
    )


def test_execute_install_plan_body_within_ratchet_budget() -> None:
    # install() is a thin delegation shim; the real orchestrator after the
    # implementation note refactor is execute_install_plan. Gate it too, or the S4
    # budget above passes trivially while orchestration bloat accumulates
    # unchecked. 230 is a ratchet pinned just above the current size (224)
    # — lower it as steps are extracted, never raise it.
    plan_mod = importlib.import_module("workstate_bootstrap.install_plan")

    count = _executable_body_lines(plan_mod.execute_install_plan)
    assert count <= 230, (
        f"execute_install_plan has {count} executable lines; "
        f"ratchet budget is 230 — extract steps instead of raising the budget"
    )


def test_migrated_fsutil_names_not_imported_as_install_privates() -> None:
    # RF29-S3-01 (implementation note implementation note): deep_merge/write_json_file live in
    # workstate_bootstrap.fsutil as public names. Scoped gate: no module
    # other than install.py may reference the legacy install privates
    # _deep_merge/_write_json_file. Other install privates are out of scope.
    root = _bootstrap_src()
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        if path.name == "install.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "_deep_merge" in text or "_write_json_file" in text:
            offenders.append(path.name)
    assert offenders == [], (
        f"legacy install-private imports of migrated fsutil helpers remain "
        f"in: {offenders}"
    )


def test_bootstrap_modules_route_subprocess_via_gateway() -> None:
    root = _bootstrap_src()
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        if path.name == "external.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "subprocess.run(" in text:
            offenders.append(path.name)
    assert offenders == [], f"direct subprocess.run remains in: {offenders}"


def test_compose_plugin_mcp_overrides_body_under_s4_budget() -> None:
    compose_path = (
        Path(__file__).resolve().parents[2]
        / "workstate-system"
        / "workstate_system"
        / "payload"
        / "scripts"
        / "plugin_override_compose.py"
    )
    source = compose_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "compose_plugin_mcp_overrides"
    )
    body_line_count = func.end_lineno - func.body[0].lineno + 1
    assert body_line_count <= 60, (
        f"compose_plugin_mcp_overrides body is {body_line_count} lines; S4 budget is 60"
    )
