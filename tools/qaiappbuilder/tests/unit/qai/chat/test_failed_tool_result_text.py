# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""A tool that RAISES must not reach the model (or the UI card) as the string "None".

Real incident (2026-11): the failing ``sm8975__get_skill`` call was stored as
``output="None", isError=false``. The handler's exception had been wrapped as
``ToolInvocationResult(ok=False, result=None, error_message=...)`` but the turn
loop read only ``.result`` -> ``str(None)``. The model therefore reported an empty
result and retried blindly, and the card looked successful.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.application.ports import ToolInvocationRequest, ToolInvocationResult
from qai.chat.application.use_cases._streaming_helpers import failed_invocation_text
from qai.chat.domain.ids import ConversationId, TabId


def _inv(**kw: Any) -> ToolInvocationResult:
    base: dict[str, Any] = dict(tool_name="t", ok=False, result=None)
    base.update(kw)
    return ToolInvocationResult(**base)


def test_failed_without_output_reports_the_error_message() -> None:
    text = failed_invocation_text(_inv(error_message="tool 'x' handler raised: boom"))
    assert text == "[tool_error] tool 'x' handler raised: boom"


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_empty_results_count_as_no_output(empty: Any) -> None:
    assert failed_invocation_text(_inv(result=empty, error_message="m")) == "[tool_error] m"


def test_falls_back_to_the_error_code_then_a_generic_message() -> None:
    assert failed_invocation_text(_inv(error_code="chat.tool_handler_failed")) == (
        "[tool_error] chat.tool_handler_failed"
    )
    assert failed_invocation_text(_inv()) == "[tool_error] tool failed with no message"


def test_successful_calls_are_left_alone() -> None:
    assert failed_invocation_text(_inv(ok=True, result="fine")) is None
    assert failed_invocation_text(_inv(ok=True, result=None)) is None  # success with no output


def test_a_failure_that_already_carries_text_is_left_alone() -> None:
    assert failed_invocation_text(_inv(result="[mcp error] bad input", error_message="m")) is None


def test_objects_without_the_expected_attributes_do_not_crash() -> None:
    assert failed_invocation_text(SimpleNamespace(ok=False)) == "[tool_error] tool failed with no message"
    assert failed_invocation_text(object()) is None  # no ``ok`` attribute -> treated as ok


def test_end_to_end_a_raising_handler_never_becomes_the_string_none() -> None:
    """Registry wrapping + the helper, exactly as the turn loop chains them."""
    tools = RegistryBackedToolInvocation()

    async def boom(request: ToolInvocationRequest) -> str:
        raise ValueError("Separator is found, but chunk is longer than limit")

    tools.register("sm8975__get_skill", boom, schema=None)

    async def go() -> ToolInvocationResult:
        return await tools.invoke(
            ToolInvocationRequest(
                tab_id=TabId("t"),
                conversation_id=ConversationId("c"),
                tool_name="sm8975__get_skill",
                arguments={"id": "x"},
            )
        )

    inv = asyncio.run(go())
    assert inv.ok is False and inv.result is None  # the shape that used to render as "None"
    shown = failed_invocation_text(inv)
    assert shown is not None
    assert shown.startswith("[tool_error]")  # the prefix the card persistence keys on
    assert "Separator is found" in shown
    assert shown != "None"
