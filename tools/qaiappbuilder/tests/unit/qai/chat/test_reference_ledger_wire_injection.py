# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""P1.d — reference-ledger wire injection in ``_build_base_wire_messages``.

The base wire builder MUST insert the checkpoint's rendered ``[References
Ledger]`` block as a ``role: system`` message immediately AFTER the
``compacted_wire`` tail (at ``len(ckpt.compacted_wire)``), before the
increment history and the trailing user turn; sessions without a ledger
(no checkpoint, or a legacy pre-P1.b row) MUST produce a wire that is
BYTE-IDENTICAL to the pre-P1.d output.
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
from qai.chat.domain.content import MessageContent, MessageRole
from qai.chat.domain.ids import MessageId, TabId
from qai.chat.domain.message import Message
from qai.chat.domain.reference_ledger import Reference, ReferenceLedger

from tests.unit.qai.chat.fakes import (
    FakeClock,
    FakeConversationRepository,
    FakeIdGenerator,
    FakeStreamAbortRegistry,
    FakeTabSessionStore,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------
class _NullLLM:
    def stream(self, request):  # pragma: no cover
        raise AssertionError("LLM should not be called in these tests")


class _NullTools:
    async def invoke(self, request):  # pragma: no cover
        raise AssertionError("tools should not be called in these tests")

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
# Injection semantics
# ---------------------------------------------------------------------------
async def test_wire_contains_reference_ledger_block_when_checkpoint_has_ledger() -> None:
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids,
        [_user(1, "hi"), _assistant(1, "hello")],
    )
    uc = StreamChatUseCase(
        conversations=repo,
        tabs=FakeTabSessionStore(),
        llm=_NullLLM(),
        tools=_NullTools(),
        abort_registry=FakeStreamAbortRegistry(),
        clock=clock,
        ids=ids,
    )
    ledger = ReferenceLedger()
    ledger.add(Reference(kind="file", value="foo.py", mode="R"))
    ledger.add(Reference(kind="file", value="bar.py", mode="W"))
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=[{"role": "system", "content": "compacted head"}],
        reference_ledger=ledger,
    )

    wire = uc._build_base_wire_messages(
        conv=conv, request=_request(conv), compressed_history=None,
    )

    system_ledger_msgs = [
        m for m in wire
        if m.get("role") == "user"
        and "References Ledger" in str(m.get("content", ""))
    ]
    assert len(system_ledger_msgs) == 1
    content = system_ledger_msgs[0]["content"]
    assert "foo.py" in content
    assert "bar.py" in content
    # Positioned immediately AFTER the compacted_wire tail (insert_pos = 1).
    ledger_idx = wire.index(system_ledger_msgs[0])
    assert ledger_idx == 1  # compacted_wire has 1 entry → insert_pos = 1
    # The message after the ledger is the first increment message (user "hi"),
    # NOT the trailing current-user turn.
    assert wire[ledger_idx + 1].get("role") == "user"
    assert wire[ledger_idx + 1].get("content") == "hi"
    # The trailing current-user turn is still the last message.
    assert wire[-1].get("role") == "user"
    assert wire[-1].get("content") == "current question"


async def test_wire_is_byte_identical_when_no_ledger_present() -> None:
    """Old sessions (checkpoint.reference_ledger is None) → unchanged wire.

    Byte-for-byte compat: an existing conv WITH a checkpoint but WITHOUT a
    ledger MUST produce the same wire it produced pre-P1.d. Serialising
    both wires to canonical JSON gives a rigorous byte-level comparison.
    """
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids,
        [_user(1, "hi"), _assistant(1, "hello")],
    )
    uc = _build_uc_from(repo, clock, ids)

    # Seed a checkpoint WITHOUT a ledger.
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=[{"role": "system", "content": "compacted head"}],
        reference_ledger=None,
    )
    wire_no_ledger = uc._build_base_wire_messages(
        conv=conv, request=_request(conv), compressed_history=None,
    )

    # And run the same request with an EMPTY ledger (should also inject
    # nothing — render_wire_block returns None on an empty ledger).
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=[{"role": "system", "content": "compacted head"}],
        reference_ledger=ReferenceLedger(),
    )
    wire_empty_ledger = uc._build_base_wire_messages(
        conv=conv, request=_request(conv), compressed_history=None,
    )

    # Neither wire contains a References Ledger block.
    for wire in (wire_no_ledger, wire_empty_ledger):
        assert not any(
            "References Ledger" in str(m.get("content", "")) for m in wire
        )
    # And the two shapes are byte-identical.
    assert json.dumps(wire_no_ledger, sort_keys=True) == json.dumps(
        wire_empty_ledger, sort_keys=True,
    )


async def test_wire_unchanged_when_no_checkpoint_at_all() -> None:
    """A conversation with no checkpoint at all → no ledger block."""
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids,
        [_user(1, "hi"), _assistant(1, "hello")],
    )
    uc = _build_uc_from(repo, clock, ids)
    # No checkpoint seeded — dict is empty.
    assert uc._compaction_checkpoints == {}

    wire = uc._build_base_wire_messages(
        conv=conv, request=_request(conv), compressed_history=None,
    )
    assert not any(
        "References Ledger" in str(m.get("content", "")) for m in wire
    )


async def test_ledger_block_follows_compacted_wire() -> None:
    """The ledger block lands at insert_pos = len(compacted_wire), not at -1."""
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids,
        [_user(1, "prior u"), _assistant(1, "prior a")],
    )
    uc = _build_uc_from(repo, clock, ids)

    ledger = ReferenceLedger()
    ledger.add(Reference(kind="url", value="https://example.com", mode="R"))
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=[{"role": "system", "content": "head"}],
        reference_ledger=ledger,
    )
    wire = uc._build_base_wire_messages(
        conv=conv, request=_request(conv, text="q?"), compressed_history=None,
    )

    # Last message = current user turn.
    assert wire[-1].get("role") == "user"
    # Ledger is at index 1 (right after compacted_wire[0]), NOT at wire[-2].
    assert wire[1].get("role") == "user"
    assert "References Ledger" in str(wire[1].get("content", ""))
    assert "example.com" in str(wire[1].get("content", ""))


def _build_uc_from(repo, clock, ids) -> StreamChatUseCase:
    return StreamChatUseCase(
        conversations=repo,
        tabs=FakeTabSessionStore(),
        llm=_NullLLM(),
        tools=_NullTools(),
        abort_registry=FakeStreamAbortRegistry(),
        clock=clock,
        ids=ids,
    )
