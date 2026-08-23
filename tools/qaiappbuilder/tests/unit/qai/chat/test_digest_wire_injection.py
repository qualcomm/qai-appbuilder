# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""P2 wire injection — ``[Session Digest]`` system message before the user turn.

The base wire builder MUST insert a ``role: system`` message carrying the
checkpoint's ``digest_text`` (prefixed by ``[Session Digest]\\n``) immediately
AFTER the ``compacted_wire`` tail (at ``len(ckpt.compacted_wire)``), before
the increment history and the trailing user turn. When the digest is
``None`` / empty AND no ledger exists, the wire is byte-identical to
pre-P2 output. When both digest and ledger are present, the digest lands
BEFORE the ledger (higher-level context first), both immediately after the
compacted head.
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


class _NullLLM:
    def stream(self, request):  # pragma: no cover
        raise AssertionError("LLM should not be called in these tests")


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
# Digest-present injection
# ---------------------------------------------------------------------------
async def test_digest_text_injected_as_system_before_user() -> None:
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids,
        [_user(1, "hi"), _assistant(1, "hello")],
    )
    uc = _build_uc()
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=[{"role": "system", "content": "head"}],
        digest_text="## Progress\n### Done\n- [x] Task A",
    )

    wire = uc._build_base_wire_messages(
        conv=conv, request=_request(conv), compressed_history=None,
    )
    digest_msgs = [
        m for m in wire
        if m.get("role") == "user"
        and "Session Digest" in str(m.get("content", ""))
    ]
    assert len(digest_msgs) == 1
    # Prefixed with the tag line.
    content = digest_msgs[0]["content"]
    assert content.startswith("[Session Digest]\n")
    assert "Task A" in content
    # Sits immediately after the compacted_wire tail (insert_pos = 1 here),
    # before the increment history.
    idx = wire.index(digest_msgs[0])
    assert idx == 1  # compacted_wire has 1 entry → insert_pos = 1
    # The message after the digest is the first increment message (user "hi"),
    # NOT the trailing current-user turn.
    assert wire[idx + 1].get("role") == "user"
    assert wire[idx + 1].get("content") == "hi"
    # The trailing current-user turn is still the last message.
    assert wire[-1].get("role") == "user"
    assert wire[-1].get("content") == "current question"


async def test_digest_text_none_leaves_wire_unchanged() -> None:
    """Byte-identical wire when the checkpoint has no digest AND no ledger."""
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids,
        [_user(1, "hi"), _assistant(1, "hello")],
    )
    uc = _build_uc()
    # First: no checkpoint at all.
    wire_no_ckpt = uc._build_base_wire_messages(
        conv=conv, request=_request(conv), compressed_history=None,
    )
    # Then: a checkpoint with BOTH digest_text=None AND no ledger.
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=[{"role": "system", "content": "head"}],
        digest_text=None,
        reference_ledger=None,
    )
    wire_none_digest = uc._build_base_wire_messages(
        conv=conv, request=_request(conv), compressed_history=None,
    )
    # No Session Digest block in either.
    for wire in (wire_no_ckpt, wire_none_digest):
        assert not any(
            "Session Digest" in str(m.get("content", "")) for m in wire
        )


async def test_empty_digest_string_is_not_injected() -> None:
    """A whitespace-only digest string is treated as absent."""
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids,
        [_user(1, "hi"), _assistant(1, "hello")],
    )
    uc = _build_uc()
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=[{"role": "system", "content": "head"}],
        digest_text="   \n\t  \n",
    )
    wire = uc._build_base_wire_messages(
        conv=conv, request=_request(conv), compressed_history=None,
    )
    assert not any(
        "Session Digest" in str(m.get("content", "")) for m in wire
    )


async def test_digest_precedes_ledger_when_both_present() -> None:
    """The digest system message lands BEFORE the ledger system message."""
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids,
        [_user(1, "hi"), _assistant(1, "hello")],
    )
    uc = _build_uc()
    ledger = ReferenceLedger()
    ledger.add(Reference(kind="url", value="https://example.com"))
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=[{"role": "system", "content": "head"}],
        digest_text="## Goal\n- Do X",
        reference_ledger=ledger,
    )
    wire = uc._build_base_wire_messages(
        conv=conv, request=_request(conv, text="q?"), compressed_history=None,
    )
    # Blocks land at insert_pos = 1 (right after compacted_wire[0]).
    # Wire: [head, digest, ledger, user_hi, assistant_hello, current_user_q?]
    assert wire[-1].get("role") == "user"
    assert wire[-1].get("content") == "q?"
    # digest at index 1, ledger at index 2.
    assert wire[1].get("role") == "user"
    assert "Session Digest" in str(wire[1].get("content", ""))
    assert wire[2].get("role") == "user"
    assert "References Ledger" in str(wire[2].get("content", ""))


# ---------------------------------------------------------------------------
# Insertion-position correctness (sync-path audit)
# ---------------------------------------------------------------------------
async def test_override_path_does_not_insert_into_live_history() -> None:
    """``compressed_history`` override: blocks must NOT land at
    ``len(compacted_wire)``.

    On the synthetic-retry path the base wire is the caller's own history, so
    no prefix of it is the compacted head. Indexing by
    ``len(ckpt.compacted_wire)`` sliced the override's OWN messages apart (and,
    when the override was shorter than the head, pushed the blocks AFTER the
    trailing user turn — an Anthropic-invalid wire that does not end on a user
    message). The blocks belong at the front instead.
    """
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids, [_user(1, "hi"), _assistant(1, "hello")],
    )
    uc = _build_uc()
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=4,
        compacted_wire=[
            {"role": "system", "content": "HEAD-0"},
            {"role": "user", "content": "HEAD-1"},
            {"role": "assistant", "content": "HEAD-2"},
        ],
        digest_text="DIGEST-BODY",
    )
    override = (
        {"role": "user", "content": "OV-0"},
        {"role": "assistant", "content": "OV-1"},
        {"role": "user", "content": "OV-NUDGE"},
    )
    wire = uc._build_base_wire_messages(
        conv=conv, request=_request(conv, text="now?"),
        compressed_history=override,
    )
    # The override's own three messages stay CONTIGUOUS (nothing spliced in).
    texts = [str(m.get("content")) for m in wire]
    assert texts.index("OV-1") == texts.index("OV-0") + 1
    assert texts.index("OV-NUDGE") == texts.index("OV-1") + 1
    # The wire still ends on the user turn.
    assert wire[-1].get("role") == "user"
    assert wire[-1].get("content") == "now?"
    # And the digest precedes the history it is attached to.
    digest_idx = next(
        i for i, m in enumerate(wire)
        if "Session Digest" in str(m.get("content", ""))
    )
    assert digest_idx < texts.index("OV-0")


async def test_override_shorter_than_head_keeps_user_turn_last() -> None:
    """A short override must not push system blocks past the user turn."""
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids, [_user(1, "hi"), _assistant(1, "hello")],
    )
    uc = _build_uc()
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=4,
        compacted_wire=[
            {"role": "system", "content": "HEAD-0"},
            {"role": "user", "content": "HEAD-1"},
            {"role": "assistant", "content": "HEAD-2"},
        ],
        digest_text="DIGEST-BODY",
    )
    wire = uc._build_base_wire_messages(
        conv=conv, request=_request(conv, text="now?"),
        compressed_history=({"role": "user", "content": "OV-ONLY"},),
    )
    assert wire[-1].get("role") == "user"
    assert wire[-1].get("content") == "now?"


async def test_blocks_never_split_an_atomic_tool_group() -> None:
    """A head ending mid tool-group must not orphan the paired ``role:tool``.

    ``repair_orphan_tool_messages`` resets its open-tool-id set on ANY
    non-assistant row, so a ``role: system`` block spliced between an
    ``assistant{tool_calls}`` and its replies re-classifies those replies as
    orphans and FOLDS them into the injected block — deleting the tool rows.
    The insertion position must walk back to the group head instead.
    """
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids, [_user(1, "hi"), _assistant(1, "hello")],
    )
    uc = _build_uc()
    head = [
        {"role": "user", "content": "HEAD-q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "X",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        },
    ]
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=list(head),
        digest_text="DIGEST-BODY",
    )
    # The increment opens with the reply that pairs with the head's last call.
    uc._assemble_history_wire = lambda *, conv, request: [  # type: ignore[method-assign]
        *[dict(m) for m in head],
        {"role": "tool", "tool_call_id": "X", "content": "RESULT-KEEP-ME"},
        {"role": "assistant", "content": "done"},
    ]
    wire = uc._build_base_wire_messages(
        conv=conv, request=_request(conv, text="now?"), compressed_history=None,
    )
    # The tool reply SURVIVES as a real role:tool row, still paired with X.
    tool_rows = [m for m in wire if m.get("role") == "tool"]
    assert len(tool_rows) == 1
    assert tool_rows[0].get("tool_call_id") == "X"
    assert tool_rows[0].get("content") == "RESULT-KEEP-ME"
    # The opener is immediately followed by its reply (group not split).
    opener_idx = next(
        i for i, m in enumerate(wire) if m.get("tool_calls")
    )
    assert wire[opener_idx + 1] is tool_rows[0]
    # The digest is still present, and NOT polluted with folded tool text.
    digest = next(
        m for m in wire if "Session Digest" in str(m.get("content", ""))
    )
    assert "[Tool Result]" not in str(digest.get("content"))


async def test_round_zero_wire_carries_the_same_blocks_as_followup() -> None:
    """三口径归一: the round-0 send wire must carry the digest too.

    ``_build_llm_request``'s checkpoint branch built the ROUND-0 wire without
    the prefix blocks while ``_build_base_wire_messages`` (rounds 1+) injected
    them — the model saw two different wires inside one turn, and the trigger
    gate under-counted the real send by the whole block size.
    """
    repo = FakeConversationRepository()
    clock = FakeClock()
    ids = FakeIdGenerator()
    conv = await _make_conv(
        repo, clock, ids, [_user(1, "hi"), _assistant(1, "hello")],
    )
    uc = _build_uc()
    ledger = ReferenceLedger()
    ledger.add(Reference(kind="file", value="src/a.py"))
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=[{"role": "system", "content": "head"}],
        digest_text="## Progress\n- [x] Task A",
        reference_ledger=ledger,
    )
    req = _request(conv, text="now?")
    tab = type("_Tab", (), {"id": TabId("tab-1")})()
    llm_req = await uc._build_llm_request(
        conv=conv, tab=tab, request=req, compressed_history=None,
    )
    round0 = (llm_req.extra or {}).get("messages") or []
    followup = uc._build_base_wire_messages(
        conv=conv, request=req, compressed_history=None,
    )
    assert round0, "round-0 branch must assemble an explicit messages override"
    for wire in (round0, followup):
        assert any(
            "Session Digest" in str(m.get("content", "")) for m in wire
        )
        assert any(
            "References Ledger" in str(m.get("content", "")) for m in wire
        )
    # Same head, same block position in both口径.
    assert [m.get("role") for m in round0[:5]] == [
        m.get("role") for m in followup[:5]
    ]