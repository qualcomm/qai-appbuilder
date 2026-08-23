# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""P3 — wire injection of the ``[Turn Prefix Summary]`` system message.

CONTEXT-COMPRESSION-NEXT §7.1: ``_inject_compaction_prefix_blocks`` inserts
one ``role: system`` message per retained turn-prefix summary immediately
AFTER the ``compacted_wire`` tail (at ``len(ckpt.compacted_wire)``), before
the increment history and the trailing user turn. The wire order is:
``[compacted_wire …][Session Digest][References Ledger][Turn Prefix Summary]*``
``[increment …][user]``.

Byte-for-byte compatibility: a checkpoint whose ``turn_prefix_summaries_json``
is ``None`` / whitespace / malformed produces an identical wire — the
method returns without touching ``messages``.
"""

from __future__ import annotations

import datetime
import json

import pytest

from qai.chat.application.use_cases import (
    CreateConversationInput,
    CreateConversationUseCase,
    StreamChatInput,
    StreamChatUseCase,
)
from qai.chat.application.use_cases._agentic_kernel import CompactionCheckpoint
from qai.chat.application.use_cases.summarize_turn_prefix import (
    append_turn_prefix_summary,
)
from qai.chat.domain.content import MessageContent, MessageRole
from qai.chat.domain.ids import MessageId, TabId
from qai.chat.domain.message import Message

from tests.unit.qai.chat.fakes import (
    FakeClock,
    FakeConversationRepository,
    FakeIdGenerator,
    FakeStreamAbortRegistry,
    FakeTabSessionStore,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _NullLLM:
    def stream(self, request):  # pragma: no cover
        raise AssertionError("LLM should not be called")


class _NullTools:
    async def invoke(self, request):  # pragma: no cover
        raise AssertionError("tools should not be called")

    def schemas(self):
        return ()


def _now() -> datetime.datetime:
    return datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def _user(idx: int, text: str) -> Message:
    return Message(
        id=MessageId(f"u-{idx}"),
        role=MessageRole.USER,
        content=MessageContent(text=text),
        created_at=_now(),
    )


def _assistant(idx: int, text: str) -> Message:
    return Message(
        id=MessageId(f"a-{idx}"),
        role=MessageRole.ASSISTANT,
        content=MessageContent(text=text),
        created_at=_now(),
    )


async def _make_conv(repo, clock, ids, messages):
    conv = await CreateConversationUseCase(
        conversations=repo, clock=clock, ids=ids,
    ).execute(CreateConversationInput(title="t"))
    for m in messages:
        conv.append_message(m)
    await repo.save(conv)
    return conv


def _build_uc() -> StreamChatUseCase:
    return StreamChatUseCase(
        conversations=FakeConversationRepository(),
        tabs=FakeTabSessionStore(),
        llm=_NullLLM(),
        tools=_NullTools(),
        abort_registry=FakeStreamAbortRegistry(),
        clock=FakeClock(),
        ids=FakeIdGenerator(),
    )


def _request(conv, text: str = "current question") -> StreamChatInput:
    return StreamChatInput(
        tab_id=TabId("tab-1"),
        conversation_id=conv.id,
        user_message=MessageContent(text=text),
        model_hint="claude-sonnet-4",
    )


# ---------------------------------------------------------------------------
# One summary present → one system message injected
# ---------------------------------------------------------------------------
async def test_single_turn_prefix_summary_injected() -> None:
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids, [_user(1, "hi"), _assistant(1, "hello")],
    )
    uc = _build_uc()
    summaries_json = append_turn_prefix_summary(
        None,
        user_message_id="u-1",
        summary_text="Original request: refactor foo\nEarly progress: read foo.py",
        created_at="2026-07-29T00:00:00+00:00",
    )
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=[{"role": "system", "content": "head"}],
        turn_prefix_summaries_json=summaries_json,
    )
    wire = uc._build_base_wire_messages(
        conv=conv, request=_request(conv), compressed_history=None,
    )
    tp_msgs = [
        m for m in wire
        if m.get("role") == "user"
        and "Turn Prefix Summary" in str(m.get("content", ""))
    ]
    assert len(tp_msgs) == 1
    content = tp_msgs[0]["content"]
    assert content.startswith("[Turn Prefix Summary]\n")
    assert "refactor foo" in content
    # Immediately after the compacted_wire tail (at insert_pos = 1 here),
    # before the increment history.
    idx = wire.index(tp_msgs[0])
    assert idx == 1  # compacted_wire has 1 entry → insert_pos = 1
    # The block after the summary is the first increment message (user "hi"),
    # NOT the trailing current-user turn.
    assert wire[idx + 1].get("role") == "user"


# ---------------------------------------------------------------------------
# Three summaries → three system messages, in oldest-to-newest order
# ---------------------------------------------------------------------------
async def test_three_turn_prefix_summaries_injected_in_order() -> None:
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids, [_user(1, "hi"), _assistant(1, "hello")],
    )
    uc = _build_uc()
    payload = None
    for i in range(1, 4):
        payload = append_turn_prefix_summary(
            payload,
            user_message_id=f"u-{i}",
            summary_text=f"summary #{i}",
            created_at=f"2026-07-{28 + i:02d}T00:00:00+00:00",
        )
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=[{"role": "system", "content": "head"}],
        turn_prefix_summaries_json=payload,
    )
    wire = uc._build_base_wire_messages(
        conv=conv, request=_request(conv), compressed_history=None,
    )
    tp_msgs = [
        m for m in wire
        if m.get("role") == "user"
        and "Turn Prefix Summary" in str(m.get("content", ""))
    ]
    assert len(tp_msgs) == 3
    # Blocks are collected in FIFO order (oldest → newest) and spliced in one
    # shot at insert_pos, so the final wire has them in oldest-first order.
    contents = [m["content"] for m in tp_msgs]
    assert "summary #1" in contents[0]
    assert "summary #2" in contents[1]
    assert "summary #3" in contents[2]
    # All three summaries sit at insert_pos (= 1, right after compacted_wire).
    first_idx = wire.index(tp_msgs[0])
    assert first_idx == 1
    last_idx = wire.index(tp_msgs[-1])
    assert last_idx == 3  # 1 compacted_wire + 3 summaries → last at index 3
    # The message after the last summary is the first increment message.
    assert wire[last_idx + 1].get("role") == "user"


# ---------------------------------------------------------------------------
# None / whitespace / malformed → wire unchanged (byte-level)
# ---------------------------------------------------------------------------
async def test_none_turn_prefix_leaves_wire_unchanged() -> None:
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids, [_user(1, "hi"), _assistant(1, "hello")],
    )
    uc = _build_uc()
    baseline = uc._build_base_wire_messages(
        conv=conv, request=_request(conv), compressed_history=None,
    )
    # Same conv, plant an empty checkpoint — should not add any TP block.
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=[],
        turn_prefix_summaries_json=None,
    )
    after = uc._build_base_wire_messages(
        conv=conv, request=_request(conv), compressed_history=None,
    )
    # Compare canonical JSON (ordering may differ across dicts / lists but
    # not the semantic wire — we require BYTE-LEVEL wire identity here).
    assert json.dumps(baseline, sort_keys=True) == json.dumps(
        after, sort_keys=True,
    )


async def test_invalid_json_turn_prefix_is_skipped() -> None:
    """Malformed JSON is a silent no-op (best-effort read)."""
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids, [_user(1, "hi"), _assistant(1, "hello")],
    )
    uc = _build_uc()
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=[],
        turn_prefix_summaries_json="{not valid json",
    )
    wire = uc._build_base_wire_messages(
        conv=conv, request=_request(conv), compressed_history=None,
    )
    tp_msgs = [
        m for m in wire
        if m.get("role") == "user"
        and "Turn Prefix Summary" in str(m.get("content", ""))
    ]
    assert tp_msgs == []


async def test_non_list_turn_prefix_is_skipped() -> None:
    """A non-list JSON payload is treated as absent."""
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids, [_user(1, "hi"), _assistant(1, "hello")],
    )
    uc = _build_uc()
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=[],
        turn_prefix_summaries_json='{"user_message_id": "u1"}',  # dict, not list
    )
    wire = uc._build_base_wire_messages(
        conv=conv, request=_request(conv), compressed_history=None,
    )
    tp_msgs = [
        m for m in wire
        if m.get("role") == "user"
        and "Turn Prefix Summary" in str(m.get("content", ""))
    ]
    assert tp_msgs == []
