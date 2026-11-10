# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``chat.turn.start`` / ``chat.turn.end`` diagnostics in ``StreamChatUseCase._run``.

Every turn logs one ``start`` and exactly one ``end`` whose ``outcome`` names
the exit path, so a turn that "just stopped" can be explained from
``data/logs/backend.log`` afterwards.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator

import pytest

from qai.chat.application.ports import LLMStreamRequest
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


class _ScriptedLLM:
    def __init__(self, mode: str) -> None:
        self._mode = mode
        self.on_first_chunk = None  # optional callback run after the first chunk

    def stream(self, request: LLMStreamRequest) -> AsyncIterator[StreamFrame]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[StreamFrame]:
        usage = {"prompt_tokens": 120, "completion_tokens": 7}
        yield StreamFrame.chunk(frame_id="c-1", sequence=0, text="hello ")
        if self.on_first_chunk is not None:
            self.on_first_chunk()
        if self._mode == "hang":
            await asyncio.sleep(3600)
        if self._mode == "adapter_log":
            # an adapter-level log line: must carry the turn's tab/conversation
            from qai.platform.logging import get_logger

            get_logger("qai.test.adapter").warning("adapter.something_happened")
        if self._mode == "error_frame":
            yield StreamFrame.error(
                frame_id="err-1", sequence=1, code="chat.llm.timeout", message="timed out",
            )
            yield StreamFrame.end(frame_id="e-1", sequence=2, reason="failed")
            return
        if self._mode == "raise":
            raise RuntimeError("adapter blew up")
        yield StreamFrame.chunk(frame_id="c-2", sequence=1, text="world")
        yield StreamFrame.end(frame_id="e-1", sequence=2, usage=usage)


class _NullTools:
    async def invoke(self, request):  # pragma: no cover
        raise AssertionError("tools should not be called")

    def schemas(self):
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


async def _start_turn(mode: str, *, stall_budget_s: float | None = None):
    repo, tabs = FakeConversationRepository(), FakeTabSessionStore()
    clock, ids = FakeClock(), FakeIdGenerator()
    conv = await CreateConversationUseCase(
        conversations=repo, clock=clock, ids=ids,
    ).execute(CreateConversationInput(title="t"))
    tab = ConversationTab.open(tab_id=TabId("tab-1"), conversation_id=conv.id, now=clock.now())
    await tabs.save(tab)
    llm = _ScriptedLLM(mode)
    registry = FakeStreamAbortRegistry()
    if mode == "stop":
        # the user presses Stop while the reply is streaming
        llm.on_first_chunk = lambda: registry.abort(TabId("tab-1"), reason="user_requested")
    uc = StreamChatUseCase(
        conversations=repo,
        tabs=tabs,
        llm=llm,
        tools=_NullTools(),
        abort_registry=registry,
        clock=clock,
        ids=ids,
    )
    if stall_budget_s is not None:
        uc._frame_stall_budget_s = stall_budget_s
    agen = await uc.execute(
        StreamChatInput(
            tab_id=tab.id,
            conversation_id=conv.id,
            user_message=MessageContent(text="hi there"),
            model_hint="claude-sonnet-4",
        )
    )
    return agen, conv


async def test_completed_turn_logs_start_and_end(log_stream) -> None:
    agen, conv = await _start_turn("ok")
    async for _ in agen:
        pass

    starts = _events(log_stream, "chat.turn.start")
    ends = _events(log_stream, "chat.turn.end")
    assert len(starts) == 1 and len(ends) == 1
    assert starts[0]["conversation_id"] == conv.id.value
    assert starts[0]["model"] == "claude-sonnet-4"
    assert starts[0]["user_chars"] == len("hi there")
    end = ends[0]
    assert end["outcome"] == "completed"
    assert end["level"] == "INFO"
    assert end["tab_id"] == "tab-1"
    assert end["text_chars"] == len("hello world")
    assert end["prompt_tokens"] == 120
    assert end["completion_tokens"] == 7
    assert end["llm_error"] is None
    assert "duration_ms" in end


async def test_llm_error_frame_is_named_in_end(log_stream) -> None:
    agen, _ = await _start_turn("error_frame")
    async for _ in agen:
        pass

    ends = _events(log_stream, "chat.turn.end")
    assert len(ends) == 1
    assert ends[0]["outcome"] == "llm_error"
    assert ends[0]["llm_error"] == "chat.llm.timeout"
    assert ends[0]["level"] == "WARNING"


async def test_exception_logs_end_with_traceback(log_stream) -> None:
    agen, _ = await _start_turn("raise")
    with pytest.raises(RuntimeError):
        async for _ in agen:
            pass

    ends = _events(log_stream, "chat.turn.end")
    assert len(ends) == 1
    assert ends[0]["outcome"] == "exception"
    assert ends[0]["level"] == "ERROR"
    assert ends[0]["error_type"] == "RuntimeError"
    assert "adapter blew up" in ends[0]["error"]
    assert "Traceback" in ends[0].get("exception", "")


async def test_consumer_close_logs_end(log_stream) -> None:
    agen, _ = await _start_turn("ok")
    # read one frame, then walk away (what an SSE disconnect / early bail-out does)
    await agen.__anext__()
    await agen.aclose()

    ends = _events(log_stream, "chat.turn.end")
    assert len(ends) == 1
    assert ends[0]["outcome"] == "consumer_closed"
    assert ends[0]["level"] == "WARNING"
    assert ends[0]["frames"] == 1
    assert len(_events(log_stream, "chat.turn.start")) == 1


async def test_cancelled_task_logs_end(log_stream) -> None:
    """Page refresh / SSE disconnect: the producer task is cancelled mid-stream."""
    agen, _ = await _start_turn("hang")

    async def _drain() -> None:
        async for _ in agen:
            pass

    task = asyncio.create_task(_drain())
    for _ in range(200):  # until the turn is inside the LLM stream
        if _events(log_stream, "chat.turn.start"):
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    ends = _events(log_stream, "chat.turn.end")
    assert len(ends) == 1
    assert ends[0]["outcome"] == "cancelled"
    assert ends[0]["level"] == "WARNING"


async def test_stalled_stream_is_reported_as_stalled(log_stream) -> None:
    """The stall guard ends a silent stream; the turn must not read 'completed'."""
    agen, conv = await _start_turn("hang", stall_budget_s=0.3)
    async for _ in agen:
        pass

    ends = _events(log_stream, "chat.turn.end")
    assert len(ends) == 1
    assert ends[0]["outcome"] == "stalled"
    assert ends[0]["level"] == "WARNING"
    assert ends[0]["stall_seconds"] >= 0.3
    stalls = _events(log_stream, "chat.streaming.frame_stream_stalled")
    assert len(stalls) == 1
    # item 4: the guard's own line now names the turn
    assert stalls[0]["tab_id"] == "tab-1"
    assert stalls[0]["conversation_id"] == conv.id.value


async def test_adapter_logs_carry_turn_ids(log_stream) -> None:
    agen, conv = await _start_turn("adapter_log")
    async for _ in agen:
        pass

    (rec,) = _events(log_stream, "adapter.something_happened")
    assert rec["tab_id"] == "tab-1"
    assert rec["conversation_id"] == conv.id.value


async def test_turn_ids_are_unbound_after_the_turn(log_stream) -> None:
    from qai.platform.logging import get_logger

    agen, _ = await _start_turn("ok")
    async for _ in agen:
        pass
    get_logger("qai.test").info("after.turn")

    (rec,) = _events(log_stream, "after.turn")
    assert "tab_id" not in rec
    assert "conversation_id" not in rec


async def test_user_stop_logs_aborted(log_stream) -> None:
    agen, _ = await _start_turn("stop")
    async for _ in agen:
        pass

    ends = _events(log_stream, "chat.turn.end")
    assert len(ends) == 1
    assert ends[0]["outcome"] == "aborted"
    assert ends[0]["abort_reason"] == "user_requested"
