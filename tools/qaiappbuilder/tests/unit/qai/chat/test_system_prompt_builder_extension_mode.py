# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""A selected user-extension mode is never auto-classified as a built-in mode.

Regression: in Qualcomm Skills, a message about "QAIRT development environment
setup" matched the model-build keywords, the backend sent ``tool_mode_changed``
and the toolbar jumped to 模型构建器 (which also dropped the extension's mode).
"""

from __future__ import annotations

import pytest

from qai.chat.adapters.system_prompt_builder import RichSystemPromptBuilder
from qai.chat.application.ports import SystemPromptRequest

pytestmark = pytest.mark.unit

_TEXT = "QAIRT development environment setup requirements"


def _build(extra: dict, tool_mode: str | None = None) -> str | None:
    builder = RichSystemPromptBuilder(feature_skill_provider=lambda _: "# SKILL")
    return builder.build(
        SystemPromptRequest(tool_mode=tool_mode, extra={"latest_user_message": _TEXT, **extra})
    ).effective_tool_mode


def test_without_a_mode_the_keywords_still_promote_the_turn() -> None:
    assert _build({}) == "model_build"


@pytest.mark.parametrize("selected", [["qualcomm-skills"], ["a", "b"]])
def test_an_extension_mode_is_not_promoted(selected: list[str]) -> None:
    assert _build({"active_extensions": selected}) is None


def test_an_empty_extension_list_is_no_extension_mode() -> None:
    assert _build({"active_extensions": []}) == "model_build"


def test_an_explicit_built_in_mode_is_unchanged() -> None:
    assert _build({}, tool_mode="code") == "code"
