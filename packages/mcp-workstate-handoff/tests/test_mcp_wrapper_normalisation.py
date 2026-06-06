from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from workstate_handoff_mcp import core
from workstate_handoff_mcp.api import ToolEntry, _wrap_branch_mismatch_for_mcp


class ExampleModel(BaseModel):
    value: str


def _make_entry(name: str, result: Any) -> ToolEntry:
    def handler() -> Any:
        return result

    return ToolEntry(name=name, handler=handler, description="test")


@pytest.mark.parametrize(
    ("result", "expected_data"),
    [
        ([{"task_ref": "WORKSTATE-REF-58"}], {"rows": [{"task_ref": "WORKSTATE-REF-58"}]}),
        ("C-WORKSTATE-REF-58-0001", {"result": "C-WORKSTATE-REF-58-0001"}),
        (None, {"result": None}),
    ],
)
def test_wrapper_normalizes_non_dict_results(result: object, expected_data: dict[str, object]) -> None:
    entry = _make_entry("test_tool", result)

    wrapped = _wrap_branch_mismatch_for_mcp(entry)

    assert wrapped() == core._envelope(ok=True, tool="test_tool", data=expected_data)


def test_wrapper_passes_through_dict_result() -> None:
    result = core._envelope(ok=True, tool="test_tool", data={"result": "ok"})
    entry = _make_entry("test_tool", result)

    wrapped = _wrap_branch_mismatch_for_mcp(entry)

    assert wrapped() == result


def test_wrapper_passes_through_base_model_result() -> None:
    result = ExampleModel(value="ok")
    entry = _make_entry("test_tool", result)

    wrapped = _wrap_branch_mismatch_for_mcp(entry)

    assert wrapped() is result


def test_wrapper_rejects_unsupported_result_type() -> None:
    entry = _make_entry("unsupported_tool", 42)

    wrapped = _wrap_branch_mismatch_for_mcp(entry)

    with pytest.raises(TypeError, match="unsupported_tool"):
        wrapped()
