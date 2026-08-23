# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Mid-turn compaction round MUST keep the checkpoint prefix blocks.

``_compact_hook`` (``streaming.py``) is the kernel's step-② hook: whatever
list it returns REPLACES ``wire_messages`` in place and is streamed on the
SAME round (``_single_agent_turn.py``: ``rebuilt = await compact_hook(...)``
then ``wire_messages[:] = rebuilt``, then ``build_send_wire`` / the LLM
call).  On the ``_did_compact`` branch its base is the raw
``ckpt.compacted_wire`` — it bypasses BOTH wire builders
(``_build_llm_request`` for round 0, ``_build_base_wire_messages`` for
rounds 1+), and those builders are the only other callers of
``_inject_compaction_prefix_blocks``.

Without an explicit injector call on that branch the ``[Session Digest]`` /
``[References Ledger]`` / ``[Turn Prefix Summary]`` blocks disappear from the
compaction round's send wire AND from every round after it — the kernel keeps
GROWING the very list the hook returned, so the omission is not self-healing
on the next round.  The rounds before the compaction carry the blocks, so the
model's view of the checkpoint flips mid-turn.

The口径 asserted here is "same checkpoint state → same blocks", NOT "all three
blocks always present": the engine carries ``reference_ledger`` forward
SYNCHRONOUSLY when it stamps the new checkpoint, while ``digest_text`` /
``turn_prefix_summaries_json`` are written by fire-and-forget P2 / P3 tasks and
are therefore legitimately ``None`` on a freshly-stamped checkpoint.  So the
ledger is the field that MUST survive the compaction round.
"""

from __future__ import annotations

import datetime
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
from qai.chat.application.use_cases._agentic_kernel import CompactionCheckpoint
from qai.chat.domain.content import MessageContent, MessageRole
from qai.chat.domain.ids import MessageId, TabId
from qai.chat.domain.message import Message
from qai.chat.domain.reference_ledger import Reference, ReferenceLedger
from qai.chat.domain.stream_frame import StreamFrame
from qai.chat.domain.tab import ConversationTab

from tests.unit.qai.chat.fakes import (
    FakeClock,
    FakeConversationRepository,
    FakeIdGenerator,
    FakeStreamAbortRegistry,
    FakeTabSessionStore,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

#: Big enough that each round's tool output is a real share of the wire.
_TOOL_OUTPUT = "TOOL_OUTPUT_PAYLOAD\n" * 400

#: Provider-measured ``实发`` fed back on every round's END frame. Above the
#: 0.8 x 200K gate for ``claude-sonnet-4`` so the round-4 trigger fires from
#: the measured reading (not the coarse bytes/4 floor).
_MEASURED_PROMPT = 190_000

_LEDGER_MARKER = "[References Ledger]"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class _HeadTrimCompressor:
    """Replaces the oldest half of the head with one summary row.

    Mirrors what the production compressor does structurally (a shorter wire
    whose head is a summary), which is all this test needs — it asserts on
    the INJECTION, not on any compression ratio.
    """

    async def compress(
        self,
        messages,
        *,
        target_ratio=0.5,
        preserve_tail=4,
        budget_tokens=None,
        protect_ratio=0.35,
        wire_actual_tokens=None,
        **kwargs,  # swallow future tail-appended kwargs (AGENTS.md §3.1)
    ):
        if len(messages) <= preserve_tail:
            return [dict(m) for m in messages]
        head, tail = messages[:-preserve_tail], messages[-preserve_tail:]
        keep = head[len(head) // 2:]
        summary = {"role": "system", "content": "[compacted head summary]"}
        return [summary] + [dict(m) for m in keep] + [dict(m) for m in tail]


class _ToolLoopLLM:
    """Issues a tool call for the first ``tool_rounds`` rounds, then finishes.

    Records the ``extra["messages"]`` of every round — the bytes actually
    handed to the adapter — so the test can inspect each round's SEND wire.
    """

    def __init__(self, *, tool_rounds: int) -> None:
        self._tool_rounds = tool_rounds
        self.calls: list[LLMStreamRequest] = []
        self.sent_wires: list[list[dict]] = []

    def stream(self, request: LLMStreamRequest) -> AsyncIterator[StreamFrame]:
        self.calls.append(request)
        msgs = (request.extra or {}).get("messages")
        self.sent_wires.append([dict(m) for m in (msgs or [])])
        return self._iter(len(self.calls))

    async def _iter(self, n: int) -> AsyncIterator[StreamFrame]:
        usage = {"prompt_tokens": _MEASURED_PROMPT, "completion_tokens": 10}
        if n <= self._tool_rounds:
            yield StreamFrame.chunk(
                frame_id=f"c-{n}", sequence=0, text=f"working {n}",
            )
            yield StreamFrame.tool_call(
                frame_id=f"tc-{n}",
                sequence=1,
                tool_name="grep",
                arguments={"pattern": f"p{n}", "path": f"file_{n}.py"},
            )
            yield StreamFrame.end(frame_id=f"e-{n}", sequence=2, usage=usage)
        else:
            yield StreamFrame.chunk(
                frame_id=f"c-{n}", sequence=0, text="all done",
            )
            yield StreamFrame.end(frame_id=f"e-{n}", sequence=1, usage=usage)


class _BigOutputTools:
    async def invoke(self, request: ToolInvocationRequest) -> ToolInvocationResult:
        return ToolInvocationResult(
            tool_name=request.tool_name, ok=True, result=_TOOL_OUTPUT,
        )

    def schemas(self) -> tuple[dict, ...]:
        return ()


def _has_block(wire: list[dict], marker: str) -> bool:
    return any(
        m.get("role") == "user" and marker in str(m.get("content") or "")
        for m in wire
    )


def _block_index(wire: list[dict], marker: str) -> int:
    for idx, m in enumerate(wire):
        if m.get("role") == "user" and marker in str(m.get("content") or ""):
            return idx
    raise AssertionError(f"{marker} not present in wire")


async def _drive_turn() -> tuple[StreamChatUseCase, _ToolLoopLLM, object]:
    """Run one multi-round agentic turn whose round 4 trips compaction."""
    repo, tabs = FakeConversationRepository(), FakeTabSessionStore()
    clock, ids = FakeClock(), FakeIdGenerator()
    conv = await CreateConversationUseCase(
        conversations=repo, clock=clock, ids=ids,
    ).execute(CreateConversationInput(title="t"))
    for i in range(3):
        conv.append_message(
            Message(
                id=MessageId(f"u-{i}"),
                role=MessageRole.USER,
                content=MessageContent(text=f"old user {i} " + "x" * 200),
                created_at=_now(),
            )
        )
        conv.append_message(
            Message(
                id=MessageId(f"a-{i}"),
                role=MessageRole.ASSISTANT,
                content=MessageContent(text=f"old asst {i} " + "y" * 200),
                created_at=_now(),
            )
        )
    await repo.save(conv)
    tab = ConversationTab.open(
        tab_id=TabId("tab-1"), conversation_id=conv.id, now=clock.now(),
    )
    await tabs.save(tab)

    llm = _ToolLoopLLM(tool_rounds=6)
    uc = StreamChatUseCase(
        conversations=repo,
        tabs=tabs,
        llm=llm,
        tools=_BigOutputTools(),
        abort_registry=FakeStreamAbortRegistry(),
        clock=clock,
        ids=ids,
        guardrail_factory=lambda: InMemoryGuardrailController(),
        tool_result_truncator=AdaptiveToolResultTruncator(),
        max_followup_rounds=8,
        context_compressor=_HeadTrimCompressor(),
    )
    # Steady state: a PRIOR checkpoint already carries a ledger, so the
    # engine's synchronous merge guarantees the freshly-stamped checkpoint
    # still has one when the mid-turn compaction lands.
    ledger = ReferenceLedger()
    ledger.add(Reference(kind="file", value="seed_a.py", mode="R"))
    ledger.add(Reference(kind="file", value="seed_b.py", mode="W"))
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=[{"role": "system", "content": "[prior compacted head]"}],
        reference_ledger=ledger,
    )

    agen = await uc.execute(
        StreamChatInput(
            tab_id=tab.id,
            conversation_id=conv.id,
            user_message=MessageContent(text="investigate " + "z" * 300),
            model_hint="claude-sonnet-4",
        )
    )
    async for _ in agen:
        pass
    return uc, llm, conv


class TestCompactHookKeepsPrefixBlocks:
    async def test_ledger_block_survives_the_compaction_round(self) -> None:
        """THE REGRESSION: the compaction round and every round after it.

        Reverting the ``_inject_compaction_prefix_blocks`` call in
        ``_compact_hook``'s ``_did_compact`` branch turns every post-compaction
        round's ``ledger`` reading to ``False`` and fails here.
        """
        uc, llm, conv = await _drive_turn()
        readings = [
            _has_block(w, _LEDGER_MARKER) for w in llm.sent_wires
        ]
        # Sanity: the turn really did compact mid-flight (otherwise this test
        # would pass vacuously on the pre-compaction builder path alone).
        ckpt = uc._compaction_checkpoints[uc._conv_key(conv)]
        assert ckpt.anchor_index > 0, "mid-turn compaction did not fire"
        assert len(readings) >= 5, "turn did not run enough rounds"
        # Pre-compaction rounds carry the block (builder path).
        assert readings[0] is True
        # EVERY round carries it — no mid-turn flip.
        assert all(readings), f"ledger block missing on some round: {readings}"

    async def test_block_sits_before_the_trailing_user_of_compacted_head(
        self,
    ) -> None:
        """Pins ``head_len`` — the block goes BEFORE the trailing user of
        ``compacted_wire``.

        The compressor's ``_build_protection_plan``
        (``context_compressor.py:1197-1225``) always protects the CURRENT
        turn (last ``role:user`` + everything after it) verbatim, so
        ``ckpt.compacted_wire`` ends with — at minimum — that trailing
        user. ``_compact_hook``'s base IS ``ckpt.compacted_wire`` verbatim;
        the correct ``head_len`` is the INDEX of that trailing user, so the
        injector splices its ``[Session Digest]`` / ``[References Ledger]``
        / ``[Turn Prefix Summary]`` blocks between the compacted head and
        the trailing user — the same shape ``_build_base_wire_messages``
        produces on every non-compaction round. A wrong (e.g.
        ``len(compacted_wire)``) value would place the block AFTER the
        trailing user and push ``role:system`` to the wire tail (Anthropic
        400); a ``None`` would place it at index 0. Both fail here.
        """
        uc, llm, conv = await _drive_turn()
        ckpt = uc._compaction_checkpoints[uc._conv_key(conv)]
        # ``head_len`` = index of the LAST ``role:user`` inside compacted_wire.
        head_len = next(
            i
            for i in range(len(ckpt.compacted_wire) - 1, -1, -1)
            if ckpt.compacted_wire[i].get("role") == "user"
        )
        # First round whose wire is the post-compaction rebuild: its length
        # dropped relative to the previous round (the head was replaced).
        lengths = [len(w) for w in llm.sent_wires]
        compaction_round = next(
            i for i in range(1, len(lengths)) if lengths[i] < lengths[i - 1]
        )
        wire = llm.sent_wires[compaction_round]
        assert _block_index(wire, _LEDGER_MARKER) == head_len
        # The blocks never land after the growing tail: the rounds appended
        # after the compaction sit BEYOND the block.
        later = llm.sent_wires[-1]
        assert _block_index(later, _LEDGER_MARKER) == head_len
        assert len(later) > head_len + 1

    async def test_injection_does_not_fold_away_tool_rows(self) -> None:
        """The atomic-group hazard: no ``role:tool`` row may be swallowed.

        Splicing a ``role: system`` row between an ``assistant{tool_calls}``
        opener and its replies makes the orphan repair re-classify those
        replies as orphans and FOLD them into the injected block — silently
        deleting rows the model needs. Every ``role:tool`` row must still be
        paired with an opener that declared its ``tool_call_id``.
        """
        _uc, llm, _conv = await _drive_turn()
        for round_no, wire in enumerate(llm.sent_wires, start=1):
            declared: set[str] = set()
            for m in wire:
                if m.get("role") == "assistant":
                    for tc in m.get("tool_calls") or ():
                        if isinstance(tc, dict) and tc.get("id"):
                            declared.add(str(tc["id"]))
                elif m.get("role") == "tool":
                    tcid = str(m.get("tool_call_id") or "")
                    assert tcid in declared, (
                        f"round {round_no}: orphan role:tool {tcid!r}"
                    )

    async def test_absent_fields_inject_nothing(self) -> None:
        """口径 consistency, not unconditional presence.

        A freshly-stamped checkpoint has ``digest_text`` /
        ``turn_prefix_summaries_json`` of ``None`` (their P2 / P3 writers are
        fire-and-forget and land on a later turn), so those two blocks must be
        ABSENT from the post-compaction rounds — injecting a placeholder would
        be worse than omitting it.
        """
        uc, llm, conv = await _drive_turn()
        ckpt = uc._compaction_checkpoints[uc._conv_key(conv)]
        assert ckpt.digest_text is None
        assert ckpt.turn_prefix_summaries_json is None
        lengths = [len(w) for w in llm.sent_wires]
        compaction_round = next(
            i for i in range(1, len(lengths)) if lengths[i] < lengths[i - 1]
        )
        for wire in llm.sent_wires[compaction_round:]:
            assert not _has_block(wire, "[Session Digest]")
            assert not _has_block(wire, "[Turn Prefix Summary]")
