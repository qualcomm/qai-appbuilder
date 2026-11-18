# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Agent-only prompt mode used by ``qai agent run`` / ``qai.agent_sdk``."""

from __future__ import annotations

import pytest

from qai.chat.adapters.system_prompt_builder import (
    RichSystemPromptBuilder,
    _filter_agent_only_catalog,
    _is_agent_only_request,
)
from qai.chat.application.ports import SystemPromptRequest

pytestmark = pytest.mark.unit

_MODEL_BUILD_MESSAGE = "convert my ONNX model to QNN"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), ("true", True), ("1", True), ("off", False), (False, False), (None, False)],
)
def test_is_agent_only_request_parses_flag(value, expected) -> None:
    assert _is_agent_only_request({"agent_only": value}) is expected
    assert _is_agent_only_request(None) is False


def test_catalog_filter_drops_model_builder_rows_only() -> None:
    rows = (
        ("C:/x/chat_features/model-builder/SKILL.md", "convert models"),
        ("C:/x/chat_features/code/SKILL.md", "write code"),
    )

    assert _filter_agent_only_catalog(rows) == (rows[1],)


def test_agent_only_uses_agent_identity_and_no_model_build_fallback() -> None:
    builder = RichSystemPromptBuilder()
    result = builder.build(
        SystemPromptRequest(
            extra={"agent_only": True, "latest_user_message": _MODEL_BUILD_MESSAGE},
        )
    )

    assert "You are QAI Agent" in result.prompt
    assert result.effective_tool_mode is None


def test_default_mode_still_auto_detects_model_build() -> None:
    builder = RichSystemPromptBuilder()
    result = builder.build(
        SystemPromptRequest(extra={"latest_user_message": _MODEL_BUILD_MESSAGE})
    )

    assert "You are QAI Agent" not in result.prompt
    assert result.effective_tool_mode == "model_build"
