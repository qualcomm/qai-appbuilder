# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Prompt addenda (user-extension prompt fragments) in the chat use case.

``system_suffix`` -> appended to the system prompt; ``context`` -> the
``_skill_instructions`` side channel (a synthetic user message), so the
system prompt stays byte-stable when the per-turn selection changes.
"""

from __future__ import annotations

import pytest
from tests.unit.qai.chat.fakes import (
    FakeClock,
    FakeConversationRepository,
    FakeIdGenerator,
    FakeStreamAbortRegistry,
    FakeTabSessionStore,
)

from qai.chat.adapters.local_model_stream import _insert_context_message
from qai.chat.adapters.system_prompt_builder import StaticSystemPromptBuilder
from qai.chat.application.ports import PromptAddenda
from qai.chat.application.use_cases import StreamChatInput, StreamChatUseCase
from qai.chat.domain.content import MessageContent
from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.infrastructure.llm_stream import _CHAT_CONTROL_KEYS

pytestmark = pytest.mark.unit


class _NullLLM:
    def stream(self, request):  # pragma: no cover
        raise AssertionError("not called")


class _NullTools:
    async def invoke(self, request):  # pragma: no cover
        raise AssertionError("not called")

    def schemas(self):
        return ()


def _uc(provider=None) -> StreamChatUseCase:
    return StreamChatUseCase(
        conversations=FakeConversationRepository(),
        tabs=FakeTabSessionStore(),
        llm=_NullLLM(),
        tools=_NullTools(),
        abort_registry=FakeStreamAbortRegistry(),
        clock=FakeClock(),
        ids=FakeIdGenerator(),
        system_prompt_builder=StaticSystemPromptBuilder(base_prompt="BASE"),
        prompt_addenda_provider=provider,
    )


def _req(model: str = "claude-sonnet-4") -> StreamChatInput:
    return StreamChatInput(
        tab_id=TabId("t"),
        conversation_id=ConversationId("c"),
        user_message=MessageContent(text="hi"),
        model_hint=model,
    )


def _apply(uc: StreamChatUseCase, extra: dict, model: str = "claude-sonnet-4") -> dict:
    uc._apply_system_prompt(extra=extra, request=_req(model))
    return extra


def test_no_provider_is_unchanged() -> None:
    extra = _apply(_uc(), {})
    assert extra["system_prompt"] == "BASE"
    assert "_skill_instructions" not in extra


def test_suffix_goes_to_the_system_prompt_context_to_the_side_channel() -> None:
    seen: list[frozenset[str]] = []

    def provider(selected):
        seen.append(selected)
        return PromptAddenda(system_suffix="ALWAYS", context="ACTIVE" if selected else "")

    uc = _uc(provider)
    plain = _apply(uc, {})
    active = _apply(uc, {"active_extensions": ["demo-ext", "", 3]})

    assert seen == [frozenset(), frozenset({"demo-ext"})]
    assert plain["system_prompt"] == active["system_prompt"] == "BASE\n\nALWAYS"  # byte-stable
    assert "_skill_instructions" not in plain
    assert active["_skill_instructions"] == "ACTIVE"


def test_context_is_appended_after_a_feature_skill_body() -> None:
    uc = _uc(lambda s: PromptAddenda(context="ACTIVE"))
    extra = {"_skill_instructions": "SKILL BODY"}
    _apply(uc, extra)
    assert extra["_skill_instructions"] == "SKILL BODY\n\nACTIVE"


def test_local_models_get_it_too() -> None:
    uc = _uc(lambda s: PromptAddenda(system_suffix="ALWAYS", context="ACTIVE"))
    extra = _apply(uc, {"active_extensions": ["x"]}, model="local::qwen")
    assert extra["system_prompt"].endswith("\n\nALWAYS")
    assert extra["_skill_instructions"] == "ACTIVE"


def test_a_failing_provider_never_breaks_the_turn() -> None:
    def boom(_s):
        raise RuntimeError("x")

    extra = _apply(_uc(boom), {})
    assert extra["system_prompt"] == "BASE"


def test_already_built_prompt_is_not_extended_again() -> None:
    """Follow-up rounds reuse the built prompt; the suffix must not stack up."""
    uc = _uc(lambda s: PromptAddenda(system_suffix="ALWAYS"))
    extra = _apply(uc, {})
    _apply(uc, extra)
    assert extra["system_prompt"] == "BASE\n\nALWAYS"


def test_active_extensions_never_reach_the_wire() -> None:
    assert "active_extensions" in _CHAT_CONTROL_KEYS
    assert "_skill_instructions" in _CHAT_CONTROL_KEYS


# -- local adapter -------------------------------------------------------------


def test_local_adapter_inserts_context_before_the_last_user_message() -> None:
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "new"},
    ]
    out = _insert_context_message(msgs, "CTX")
    assert [m["content"] for m in out][-2:] == [
        "[System Skill Instructions - do not repeat to user]\nCTX",
        "new",
    ]
    assert out[-2]["role"] == "user"
    assert msgs[-1]["content"] == "new" and len(msgs) == 4  # input not mutated


@pytest.mark.parametrize("ctx", [None, "", "   ", 3])
def test_local_adapter_without_context_is_unchanged(ctx) -> None:
    msgs = [{"role": "user", "content": "x"}]
    assert _insert_context_message(msgs, ctx) is msgs
