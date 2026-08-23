# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Mid-turn compaction wire correctness — regressions for E1 & E2.

Two adjacent bugs in ``_compact_hook`` / ``_compress_via_checkpoint``:

* **E1** — ``streaming.py::_compact_hook`` ended the rebuilt wire with the
  injector's ``role:system`` block (``[References Ledger]`` / ``[Session
  Digest]`` / ``[Turn Prefix Summary]``). Anthropic-family upstreams reject a
  wire that does not end with ``role:user`` / ``role:tool`` — "does not
  support assistant message prefill; the conversation must end with a user
  message" (HTTP 400) — triggering an empty completion + synthetic-retry
  override. The fix appends the current turn's user prompt as the final row.

* **E2** — ``streaming.py::_compress_via_checkpoint``'s ``assembled``
  previously did ``list(live_wire)`` when a mid-turn compaction reused the
  kernel's ``wire_messages``. That list already carried the injector blocks
  from an earlier mid-turn compaction inside the same turn (the kernel does
  ``wire_messages[:] = rebuilt`` at ``_single_agent_turn.py:751``); the
  compressor then baked them into the fresh ``compacted_wire`` and the next
  round's injector added ANOTHER copy → duplicate ledger / digest growing
  without bound. The fix filters those three ``role:system`` prefixes out of
  ``assembled`` before compression.

Both regressions are exercised end-to-end via ``StreamChatUseCase`` with a
tool-loop LLM that drives enough mid-turn compactions to expose the bake-in.
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

_TOOL_OUTPUT = "TOOL_OUTPUT_PAYLOAD\n" * 400
# 0.8 * 200K = 160K; keep every round above the trigger so the compressor
# keeps firing round after round → second mid-turn compaction is guaranteed.
_MEASURED_PROMPT = 190_000

_LEDGER_MARKER = "[References Ledger]"
_DIGEST_MARKER = "[Session Digest]"
_TPS_MARKER = "[Turn Prefix Summary]"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class _HeadTrimCompressor:
    """Small, deterministic compressor.

    Replaces the oldest half of the head with one summary row and copies the
    rest verbatim. Mirrors the production shape (a shorter wire whose head is
    a summary) — enough to exercise the mid-turn compaction path without
    depending on token-accurate compression.

    Protection plan (mirrors production ``context_compressor.py``): the last
    ``role:user`` and everything after it is always preserved verbatim so
    multimodal / vision content in the trailing user turn is never dropped.
    ``preserve_tail`` is honoured as a minimum but the protected slice may
    extend it when the trailing user sits further back.
    """

    _counter = 0

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
        _HeadTrimCompressor._counter += 1
        n = _HeadTrimCompressor._counter
        if len(messages) <= preserve_tail:
            return [dict(m) for m in messages]
        # Find the last role:user — that is the current turn's anchor.
        # Everything from that index onward is the protected tail (mirrors the
        # production compressor's protection plan).
        last_user_idx = next(
            (i for i in range(len(messages) - 1, -1, -1)
             if messages[i].get("role") == "user"),
            None,
        )
        # Protected tail = max(preserve_tail, everything from last_user_idx).
        if last_user_idx is not None:
            protect_from = min(last_user_idx, len(messages) - preserve_tail)
        else:
            protect_from = len(messages) - preserve_tail
        head, tail = messages[:protect_from], messages[protect_from:]
        if not head:
            return [dict(m) for m in messages]
        keep = head[len(head) // 2:]
        summary = {
            "role": "system",
            "content": f"[compacted head summary #{n}]",
        }
        return [summary] + [dict(m) for m in keep] + [dict(m) for m in tail]


class _ToolLoopLLM:
    """Streams a tool call for ``tool_rounds`` rounds then finishes.

    Records ``extra["messages"]`` per round so tests can inspect what was sent.
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


def _count_marker(wire: list[dict], marker: str) -> int:
    return sum(
        1
        for m in wire
        if m.get("role") == "user" and marker in str(m.get("content") or "")
    )


def _compaction_rounds(sent_wires: list[list[dict]]) -> list[int]:
    """Return indices of rounds where a new compacted-head summary appeared.

    Wire-length shrinkage is fragile when the compressor's protection plan
    keeps more messages than the fixed ``preserve_tail`` (the protected tail
    may be longer, so the wire does not shrink). Marker-count increase is
    reliable: the compressor always prepends a ``[compacted head summary #N]``
    row, so the count in the send wire rises by 1 on the compaction round.
    """
    def _summary_count(wire: list[dict]) -> int:
        return sum(
            1 for m in wire
            if m.get("role") == "system"
            and "[compacted head summary" in str(m.get("content") or "")
        )
    return [
        i for i in range(1, len(sent_wires))
        if _summary_count(sent_wires[i]) > _summary_count(sent_wires[i - 1])
    ]


async def _drive_turn(
    *,
    tool_rounds: int = 8,
    user_text: str | None = None,
    request_extra: dict | None = None,
) -> tuple[
    StreamChatUseCase, _ToolLoopLLM, object,
]:
    """Run one long agentic turn — enough rounds to trip TWO compactions.

    The first compaction produces ``compacted_wire`` whose injector then adds
    a ``[References Ledger]`` to the kernel's ``wire_messages``. Follow-up
    rounds keep pushing the measured prompt above the 0.8 × window gate so a
    SECOND mid-turn compaction fires — that second run is what E2 targets
    (the moment ``list(live_wire)`` would have baked the ledger row into the
    fresh ``compacted_wire``).
    """
    _HeadTrimCompressor._counter = 0
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

    llm = _ToolLoopLLM(tool_rounds=tool_rounds)
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
        max_followup_rounds=tool_rounds + 2,
        context_compressor=_HeadTrimCompressor(),
    )
    # Steady state: a PRIOR checkpoint already carries a ledger, so the
    # engine's synchronous merge keeps carrying it forward across every
    # freshly-stamped checkpoint mid-turn.
    ledger = ReferenceLedger()
    ledger.add(Reference(kind="file", value="seed_a.py", mode="R"))
    ledger.add(Reference(kind="file", value="seed_b.py", mode="W"))
    uc._compaction_checkpoints[uc._conv_key(conv)] = CompactionCheckpoint(
        anchor_index=0,
        compacted_wire=[{"role": "system", "content": "[prior compacted head]"}],
        reference_ledger=ledger,
    )

    _text = user_text if user_text is not None else "investigate " + "z" * 300
    agen = await uc.execute(
        StreamChatInput(
            tab_id=tab.id,
            conversation_id=conv.id,
            user_message=MessageContent(text=_text),
            model_hint="claude-sonnet-4",
            extra=request_extra,
        )
    )
    async for _ in agen:
        pass
    return uc, llm, conv


class TestCompactHookMidTurnWireEndsWithValidRole:
    """E1 regression: every mid-turn compaction wire ends with role:user or
    role:tool.

    Anthropic-family upstreams reject a wire ending in ``role:assistant`` /
    ``role:system`` (see ``_streaming_helpers.py`` and
    ``message_sanitizer.py`` — HTTP 400 "conversation must end with a user
    message"). ``role:tool`` is legal because the API accepts a mid-turn
    tool-result as the final role (it is semantically part of the user's
    side of the turn). The prior naïve fix used
    ``_mid_head_len = len(base_wire)`` which forced the injector to splice
    its ``role:system`` prefix blocks at the tail — the trailing wire role
    then became ``role:system`` (ledger / digest / TPS) and Anthropic
    rejected. Reverting the head-len walk in ``_compact_hook`` (or removing
    the fallback trailing-role guard) flips at least one round's
    ``sent_wires[-1]["role"]`` back to ``system`` and fails here.
    """

    async def test_compact_hook_mid_turn_wire_ends_with_user_or_tool(
        self,
    ) -> None:
        uc, llm, conv = await _drive_turn()

        # Sanity: the turn really did compact mid-flight.
        ckpt = uc._compaction_checkpoints[uc._conv_key(conv)]
        assert ckpt.anchor_index > 0, "mid-turn compaction did not fire"

        # Find the compaction round(s) via marker-count increase (reliable
        # even when the compressor's protection plan keeps more messages than
        # the fixed preserve_tail and the wire does not shrink).
        compaction_rounds = _compaction_rounds(llm.sent_wires)
        assert compaction_rounds, "no mid-turn compaction round observed"

        offenders: list[tuple[int, str]] = []
        for idx in compaction_rounds:
            wire = llm.sent_wires[idx]
            assert wire, f"round {idx}: empty wire"
            last_role = wire[-1].get("role")
            if last_role not in ("user", "tool"):
                offenders.append((idx, str(last_role)))
        assert not offenders, (
            f"mid-turn compaction round(s) ended with non-user/tool role: "
            f"{offenders}"
        )


class TestMidTurnCompactionDoesNotBakeInjectedBlocks:
    """E2 regression: injector blocks NEVER survive into ``compacted_wire``.

    The compressor was fed ``list(live_wire)`` which already carried the
    previous mid-turn compaction's injected system rows. That would leak the
    rows into the fresh ``compacted_wire`` and — combined with the next
    round's injector — produce duplicate ledger / digest / TPS rows in the
    send wire.
    """

    async def test_mid_turn_compaction_does_not_bake_injected_blocks(
        self,
    ) -> None:
        # Long enough tool loop that a second mid-turn compaction fires.
        uc, llm, conv = await _drive_turn(tool_rounds=8)

        ckpt = uc._compaction_checkpoints[uc._conv_key(conv)]
        # Sanity: mid-turn compaction did fire (the fresh head is present).
        assert ckpt.anchor_index > 0, "mid-turn compaction did not fire"

        # THE regression: none of the injector's three system prefixes may
        # appear inside the compressor's output (``compacted_wire``).
        for marker in (_LEDGER_MARKER, _DIGEST_MARKER, _TPS_MARKER):
            baked = _count_marker(list(ckpt.compacted_wire), marker)
            assert baked == 0, (
                f"{marker!r} baked into compacted_wire (count={baked}); "
                f"the compressor consumed an injected block"
            )

        # And the corollary the user cares about: no send wire ends up with
        # a duplicate ledger row because of the bake-in.
        for round_no, wire in enumerate(llm.sent_wires, start=1):
            ledger_count = _count_marker(wire, _LEDGER_MARKER)
            assert ledger_count <= 1, (
                f"round {round_no}: {ledger_count} '[References Ledger]' "
                f"rows in send wire (bake-in re-introduced by injector)"
            )



class TestCompactHookMidTurnUserTurnNotDuplicated:
    """E1-v2 regression: current user turn appears at most ONCE per send wire.

    The prior fix used ``_mid_head_len = len(base_wire)``, which made the
    injector splice its ``role:system`` blocks at the tail — pushing the
    trailing user out of the last row — and then unconditionally
    ``rebuilt.append({"role":"user", ...})``. The compressor's protection
    plan keeps the CURRENT user turn verbatim inside ``compacted_wire``, so
    that unconditional append made the same user text appear TWICE per
    round from the compaction round onward. Reverting the head-len walk
    below (i.e. back to ``_mid_head_len = len(base_wire)``) flips the
    duplicate count and fails here.
    """

    async def test_compact_hook_mid_turn_wire_contains_current_user_exactly_once(
        self,
    ) -> None:
        # A high-entropy marker so the count is dependable even if the
        # compressor / injector adds unrelated ``role:user`` shims later.
        unique_text = (
            "UNIQUE_MARKER_investigate_"
            + ("zzzzzzzz" * 60)  # push over the 190K prompt gate
        )
        uc, llm, conv = await _drive_turn(
            tool_rounds=8, user_text=unique_text,
        )

        # Sanity: the turn really did compact mid-flight.
        ckpt = uc._compaction_checkpoints[uc._conv_key(conv)]
        assert ckpt.anchor_index > 0, "mid-turn compaction did not fire"

        # Find compaction rounds via marker-count increase (reliable even when
        # the compressor's protection plan keeps more messages than the fixed
        # preserve_tail and the wire does not shrink).
        compaction_rounds = _compaction_rounds(llm.sent_wires)
        assert compaction_rounds, "no mid-turn compaction round observed"

        # THE regression: from the first compaction onward, the unique
        # user text must appear AT MOST ONCE per send wire.
        first_compaction = compaction_rounds[0]
        offenders: list[tuple[int, int]] = []
        for idx in range(first_compaction, len(llm.sent_wires)):
            wire = llm.sent_wires[idx]
            count = sum(
                1 for m in wire if m.get("content") == unique_text
            )
            if count > 1:
                offenders.append((idx, count))
        assert not offenders, (
            f"current user turn duplicated in send wire (round, count) = "
            f"{offenders}"
        )


class TestCompactHookMidTurnPreservesVisionBlocks:
    """E1-v2 regression: mid-turn compaction preserves vision content blocks.

    The prior fix's fallback append used
    ``getattr(request.user_message, "text", "") or ""`` — bypassing
    :meth:`StreamChatUseCase._resolved_user_turn`, the P11 single source
    of truth that honours ``request.extra["image_content_blocks"]``. When
    the current user turn is multimodal (image + short/empty text), that
    shortcut drops every image the route layer already resolved. This
    test wires an ``image_url`` block in and drives a mid-turn
    compaction; the wire's trailing user must remain a LIST whose first
    element is the ``image_url`` block. Reverting the fallback to the
    old ``{"role":"user","content":getattr(...text...)}`` shortcut fails
    here.
    """

    async def test_compact_hook_mid_turn_preserves_vision_blocks(
        self,
    ) -> None:
        vision_block = {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64,AAA",
            },
        }
        # Non-empty text keeps the compressor's turn-parser happy on the
        # BASE messages (they still carry ``text`` for MessageContent), but
        # the request-level ``extra`` is what ``_resolved_user_turn`` reads
        # for the trailing wire user.
        uc, llm, conv = await _drive_turn(
            tool_rounds=8,
            user_text="describe this image",
            request_extra={"image_content_blocks": [vision_block]},
        )

        # Sanity: the turn really did compact mid-flight.
        ckpt = uc._compaction_checkpoints[uc._conv_key(conv)]
        assert ckpt.anchor_index > 0, "mid-turn compaction did not fire"

        # Find compaction rounds: a round whose wire contains a compacted-head
        # summary block that was NOT present in the previous round (the
        # compressor replaced the head). Length-shrinkage detection is fragile
        # when the compressor's protection plan keeps more messages than the
        # fixed preserve_tail — use marker presence instead.
        def _compacted_summary_count(wire):
            return sum(
                1 for m in wire
                if m.get("role") == "system"
                and "[compacted head summary" in str(m.get("content") or "")
            )
        compaction_rounds = [
            i for i in range(1, len(llm.sent_wires))
            if _compacted_summary_count(llm.sent_wires[i])
            > _compacted_summary_count(llm.sent_wires[i - 1])
        ]

        # THE regression: from the first compaction onward every send wire
        # MUST contain at least one ``role:user`` whose ``content`` is a
        # list starting with the ``image_url`` block. The stale shortcut
        # ``getattr(request.user_message, "text", "")`` collapses the
        # trailing user to an empty string, so this assertion flips red
        # on that revert.
        first_compaction = compaction_rounds[0]
        for idx in range(first_compaction, len(llm.sent_wires)):
            wire = llm.sent_wires[idx]
            multimodal_users = [
                m
                for m in wire
                if m.get("role") == "user"
                and isinstance(m.get("content"), list)
                and m["content"]
                and isinstance(m["content"][0], dict)
                and m["content"][0].get("type") == "image_url"
            ]
            assert multimodal_users, (
                f"round {idx}: no role:user carrying vision blocks "
                f"(content list starting with image_url) — vision was "
                f"dropped by the fallback append shortcut"
            )