# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""A tool loop whose model then answers with nothing - twice.

After a tool round the turn retries an empty reply once ("请基于工具结果总结
回答。"). When that retry is empty too, the turn must end with a visible
notice that names the request size, an ``END(reason="empty_reply")`` and a
WARNING log - not silently as "completed" (the reported "会话收到空回复后就
停止了").
"""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator

import pytest

from qai.chat.adapters.guardrail import InMemoryGuardrailController
from qai.chat.adapters.tool_result_truncator import AdaptiveToolResultTruncator
from qai.chat.application.ports import (
    LLMStreamRequest,
    ToolInvocationRequest,
    ToolInvocationResult,
)
from qai.chat.application.use_cases import (
    CreateConversationInput,
    CreateConversationUseCase,
    StreamChatInput,
    StreamChatUseCase,
)
from qai.chat.domain.content import MessageContent
from qai.chat.domain.ids import TabId
from qai.chat.domain.stream_frame import StreamFrame
from qai.chat.domain.tab import ConversationTab
from qai.platform.logging import configure_logging

from tests.unit.qai.chat.fakes import (
    FakeClock,
    FakeConversationRepository,
    FakeIdGenerator,
    FakeStreamAbortRegistry,
    FakeTabSessionStore,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_REQUEST_EST = 23456


class _ToolsThenEmptyLLM:
    """Rounds 1..tool_rounds call a tool; every later round is empty."""

    def __init__(self, *, tool_rounds: int, empty_text: str = "") -> None:
        self._tool_rounds = tool_rounds
        self._empty_text = empty_text
        self.calls = 0

    def stream(self, request: LLMStreamRequest) -> AsyncIterator[StreamFrame]:
        self.calls += 1
        return self._iter(self.calls)

    async def _iter(self, n: int) -> AsyncIterator[StreamFrame]:
        if n <= self._tool_rounds:
            yield StreamFrame.chunk(frame_id=f"c-{n}", sequence=0, text=f"working {n}")
            yield StreamFrame.tool_call(
                frame_id=f"tc-{n}", sequence=1, tool_name="grep",
                arguments={"pattern": f"p{n}", "path": f"f{n}.py"},
            )
            yield StreamFrame.end(
                frame_id=f"e-{n}", sequence=2,
                usage={"prompt_tokens": 1000 * n, "completion_tokens": 5},
            )
            return
        if self._empty_text:
            yield StreamFrame.chunk(frame_id=f"c-{n}", sequence=0, text=self._empty_text)
        yield StreamFrame.end(
            frame_id=f"e-{n}", sequence=1,
            extra={"request_est_tokens": _REQUEST_EST, "request_messages": 9},
        )


class _Tools:
    async def invoke(self, request: ToolInvocationRequest) -> ToolInvocationResult:
        return ToolInvocationResult(tool_name=request.tool_name, ok=True, result="found")

    def schemas(self) -> tuple[dict, ...]:
        return ()


@pytest.fixture
def log_stream():
    stream = io.StringIO()
    configure_logging(stream=stream, fmt="json")
    yield stream
    configure_logging(stream=io.StringIO())


def _events(stream: io.StringIO, name: str) -> list[dict]:
    out = []
    for line in stream.getvalue().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("event") == name:
            out.append(rec)
    return out


async def _run(llm: _ToolsThenEmptyLLM) -> list[StreamFrame]:
    repo, tabs = FakeConversationRepository(), FakeTabSessionStore()
    clock, ids = FakeClock(), FakeIdGenerator()
    conv = await CreateConversationUseCase(
        conversations=repo, clock=clock, ids=ids,
    ).execute(CreateConversationInput(title="t"))
    tab = ConversationTab.open(tab_id=TabId("tab-1"), conversation_id=conv.id, now=clock.now())
    await tabs.save(tab)
    uc = StreamChatUseCase(
        conversations=repo,
        tabs=tabs,
        llm=llm,
        tools=_Tools(),
        abort_registry=FakeStreamAbortRegistry(),
        clock=clock,
        ids=ids,
        guardrail_factory=lambda: InMemoryGuardrailController(),
        tool_result_truncator=AdaptiveToolResultTruncator(),
        max_followup_rounds=10,
    )
    agen = await uc.execute(
        StreamChatInput(
            tab_id=tab.id,
            conversation_id=conv.id,
            user_message=MessageContent(text="find it"),
            model_hint="claude-sonnet-4",
        )
    )
    return [f async for f in agen]


async def test_empty_after_retry_shows_notice_with_request_size(log_stream) -> None:
    llm = _ToolsThenEmptyLLM(tool_rounds=2)
    frames = await _run(llm)

    # 2 tool rounds + the empty round + the one retry, and no further rounds.
    assert llm.calls == 4
    texts = "".join(
        str(f.payload.get("text") or "") for f in frames if f.frame_type.value == "chunk"
    )
    assert str(_REQUEST_EST) in texts
    assert "2000" in texts  # last provider-measured prompt
    ends = [f.payload.get("reason") for f in frames if f.frame_type.value == "end"]
    assert "empty_reply" in ends

    (warn,) = _events(log_stream, "chat.empty_reply_after_retry")
    assert warn["level"] == "WARNING"
    assert warn["request_est_tokens"] == _REQUEST_EST
    assert warn["request_size_source"] == "request_body"
    assert warn["last_prompt_tokens"] == 2000
    assert warn["context_limit"] > 0

    (end,) = _events(log_stream, "chat.turn.end")
    assert end["outcome"] == "empty_reply"
    assert end["level"] == "WARNING"


async def test_retry_that_answers_ends_normally(log_stream) -> None:
    class _AnswersOnRetry(_ToolsThenEmptyLLM):
        async def _iter(self, n: int):
            if n == 4:
                yield StreamFrame.chunk(frame_id="c-4", sequence=0, text="summary")
                yield StreamFrame.end(frame_id="e-4", sequence=1)
                return
            async for f in super()._iter(n):
                yield f

    frames = await _run(_AnswersOnRetry(tool_rounds=2))
    ends = [f.payload.get("reason") for f in frames if f.frame_type.value == "end"]
    assert "empty_reply" not in ends
    assert not _events(log_stream, "chat.empty_reply_after_retry")
    (end,) = _events(log_stream, "chat.turn.end")
    assert end["outcome"] == "completed"
