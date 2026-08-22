# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ForceCompactChatUseCase`` -- force an immediate compaction pass.

Companion to :class:`GetContextDecisionUseCase` (the read-only
decision). This use case actually FIRES a compaction on demand, bypassing the
threshold gate, and returns a structured before/after summary so the caller
(channel bridge / REST route) can echo the reclaim to the user.

Pure application code: reads the conversation, drives the injected
:class:`~qai.chat.application.use_cases._compaction_engine.CompactionCheckpointEngine`
in ``force=True`` mode over the rebuilt full-history wire (含 role:tool
outputs — the same wire the streaming loop feeds to the model), then reads
back the resulting checkpoint's byte footprint to compute occupancy against
the model's real context window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from qai.chat.application._token_estimate_helpers import (
    authoritative_provider_size,
    coarse_byte_estimate,
    compute_overhead_from_last_wire_measurement,
)
from qai.chat.application.ports import ConversationRepositoryPort
from qai.chat.application.use_cases._agentic_kernel import estimate_wire_tokens
from qai.chat.domain.content import compute_context_usage
from qai.chat.domain.errors import InvalidContextSizeError
from qai.chat.domain.ids import ConversationId
from qai.chat.domain.model_profiles import get_context_limit
from qai.chat.application.use_cases._streaming_helpers import (
    compute_turn_prefix_at_boundary as _compute_turn_prefix_at_boundary,
    rebuild_history_wire_messages,
)
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.chat.application.use_cases._compaction_engine import (
        CompactionCheckpointEngine,
    )

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class ForceCompactChatInput:
    """Inputs for :meth:`ForceCompactChatUseCase.execute`."""

    conversation_id: ConversationId
    #: Optional model id — resolves the true context window. When ``None``
    #: the caller-supplied ``budget_tokens`` is honoured verbatim.
    model_id: str | None = None
    #: Fallback budget when no model id is supplied.
    budget_tokens: int = 200_000


@dataclass(frozen=True, slots=True, kw_only=True)
class ForceCompactChatResult:
    """Outcome of a forced compaction.

    ``before_tokens`` / ``after_tokens`` are the ESTIMATED wire size (bytes/4
    upgraded to tiktoken past ``BPE_THRESHOLD_BYTES``) before and after the
    compaction pass — the same口径 the compaction TRIGGER uses. ``savings``
    is the delta (never negative). ``budget_tokens`` is the model's true
    context window when a ``model_id`` was supplied; otherwise the caller
    fallback. ``ratio_after`` is ``after_tokens / budget_tokens`` (may exceed
    1.0 on a compaction that could not fit inside the window — the caller
    surfaces that as an escalation hint).
    """

    before_tokens: int
    after_tokens: int
    savings: int
    budget_tokens: int
    ratio_after: float
    #: ``"ok"`` when the reclaim converged and no handoff is suggested;
    #: ``"suggest_handoff"`` when the engine's mid-turn counter + digest
    #: gate says the escape hatch is the safer bet (mirrors
    #: :class:`ContextDecisionResult.escalation` — same predicate).
    escalation: Literal["ok", "suggest_handoff"] = "ok"
    #: ``True`` when the engine actually produced a new checkpoint;
    #: ``False`` when the pass was a no-op (below preserve-tail, engine
    #: unwired, or compressor failure — best-effort).
    compacted: bool = False


class ForceCompactChatUseCase:
    """Force a compaction pass immediately + report before/after tokens.

    Reads the source conversation, rebuilds the full-history wire (含
    role:tool outputs) via :func:`rebuild_history_wire_messages`, then drives
    :meth:`~CompactionCheckpointEngine.maybe_compress` in ``force=True`` mode.
    The engine writes through to its durable checkpoint store on success;
    this use case does NOT mutate ``conv.messages`` (the source history is
    preserved so the user can still audit it).
    """

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        compaction_engine: "CompactionCheckpointEngine",
    ) -> None:
        self._conversations = conversations
        self._compaction_engine = compaction_engine

    async def execute(
        self, request: ForceCompactChatInput,
    ) -> ForceCompactChatResult:
        if request.budget_tokens <= 0:
            raise InvalidContextSizeError(
                f"budget_tokens must be > 0, got {request.budget_tokens}",
            )
        budget = request.budget_tokens
        if request.model_id:
            budget = get_context_limit(request.model_id)

        conv = await self._conversations.get(request.conversation_id)
        raw_messages = getattr(conv, "messages", None) or ()

        # Rebuild the same OpenAI wire shape the streaming loop uses so
        # compaction sees ``assistant{tool_calls}`` + paired ``role:tool``
        # replies (Phase 1 can then strip the tool scaffolding cheaply).
        wire = list(rebuild_history_wire_messages(tuple(raw_messages)))

        model_hint = request.model_id or ""
        before_tokens = estimate_wire_tokens(wire, model_hint=model_hint)

        # Provider-measured wire size for the compressor's density hint.
        # Audit §3.3 single-truth-source: pick the base via
        # :func:`authoritative_provider_size`, the SAME helper the trigger
        # gate + badge + A′ reverse-compute use — so the ``eff_send_tokens``
        # field logged by the engine's ``chat.compaction.trigger`` event
        # carries the ONE caliber all four paths agree on.
        #
        # DEGRADED HANDLING: a legacy usage row (no ``first_round_prompt_
        # tokens``) yields ``degraded=True``. Force-compact does NOT gate
        # on the size (``force=True`` short-circuits the trigger), so the
        # only role ``measured_eff_prompt`` plays here is feeding the
        # compressor's density factor. On a degraded reading we pass
        # ``None`` — the compressor's fallback path uses a fixed bytes/
        # token density, which is strictly safer than feeding it the
        # poisoned ``last_round_prompt_tokens`` (audit §3.5 class: an
        # inflated density would over-shrink the wire on the SAME
        # multi-round agentic pattern §10.F called out).
        #
        # No anchor filter: force-compact walks the FULL history (a
        # ``/compact`` command from any moment, including before the
        # first-ever compaction). Passing ``checkpoint_anchor_message_id
        # =None`` mirrors the pre-audit un-filtered call.
        measured_eff_prompt: int | None = None
        _reading = authoritative_provider_size(
            conv=conv,
            checkpoint_anchor_message_id=None,
            request_model_id=request.model_id,
        )
        if _reading is not None and not _reading.degraded:
            measured_eff_prompt = _reading.base_size
        presend_eff_fallback = (
            coarse_byte_estimate(conv, request.model_id)
            if raw_messages
            else 0
        )

        checkpoint_key = conv.id.value
        prior_checkpoint = self._compaction_engine.get(checkpoint_key)
        prior_anchor = (
            prior_checkpoint.anchor_index
            if prior_checkpoint is not None
            else len(wire)
        )
        # The NEW anchor's message id — the LAST message folded into this
        # compacted head (``raw_messages[new_anchor_index - 1]``), derived the
        # same way the streaming path does
        # (``streaming.py:_compress_via_checkpoint``).
        #
        # This MUST be recomputed, not inherited from ``prior_checkpoint``:
        # ``estimate_compacted_tokens`` passes it to
        # ``_last_assistant_with_usage(after_message_id=...)`` to skip the
        # PRE-compaction assistant turn whose provider-measured
        # ``last_round_prompt_tokens`` describes the OLD (uncompacted) wire.
        # Inheriting the stale id left that turn visible, so the composer
        # badge kept showing the pre-compaction size ("发给模型 ~122.0K")
        # while the ``/compact`` reply reported the fresh one ("→ 85811") —
        # two contradictory numbers on one screen. ``None`` when the history
        # is empty (the filter degrades to a no-op).
        new_anchor_index = len(raw_messages)
        anchor_message_id: str | None = None
        if new_anchor_index > 0:
            try:
                _anchor_msg = raw_messages[new_anchor_index - 1]
                anchor_message_id = getattr(
                    getattr(_anchor_msg, "id", None), "value", None,
                )
            except Exception:  # noqa: BLE001 — degrade to no anchor filter
                anchor_message_id = None

        # P0-2 (Task O): extract references from the wire BEFORE calling
        # ``maybe_compress`` so the engine's ledger merge picks up THIS
        # turn's tool_call arguments. Without this the ledger stays empty
        # across every ``/compact`` invocation and the reference block never
        # reaches the wire. See :func:`extract_from_wire_messages` for the
        # extraction口径 (shared with the streaming and sub-agent paths).
        from qai.chat.application.reference_extractor import (
            extract_from_wire_messages as _extract_refs_from_wire,
        )
        _new_refs = _extract_refs_from_wire(wire)
        # §八 / §8.11: reverse-compute the runtime wire overhead (system
        # prompt + tool schemas + persona + skill instructions) from the last
        # PRE-COMPACT provider-measured ``last_round_prompt_tokens``. Threaded
        # into ``maybe_compress`` so the engine's stashed
        # ``estimated_tokens`` matches the branch-X figure the badge will
        # report on the next turn — no bootstrap-window jitter. ``after_tokens``
        # below folds the SAME overhead so the ``/compact`` reply and the
        # badge agree on one number (docs §8.5 selection 1). Guarded: bad /
        # missing signals collapse to ``overhead_pre = 0`` and the whole path
        # falls back to the pre-fix byte-for-byte behaviour.
        # CALIBER (§8.11 follow-up): ``wire_estimate_before`` MUST be the
        # estimate of the wire the provider ACTUALLY measured on the last
        # turn, because the reverse-computation is
        # ``overhead = last_round_prompt_tokens - wire_estimate_before``.
        # ``before_tokens`` above is the FULL RAW history (every message
        # rebuilt verbatim) — on a conversation with a standing checkpoint
        # that is far LARGER than what was sent (the checkpoint's compacted
        # head replaces the raw prefix), so the subtraction always went
        # negative and tripped the helper's ``overhead < 0`` guard →
        # ``fallback_out_of_range`` → ``overhead_pre = 0``. The checkpoint
        # then stashed ``estimated_tokens`` WITHOUT the overhead fold-in, so
        # the badge sidecar's mirror-image subtraction
        # (``streaming.py:compacted_badge_detail``) recovered 0 and reported
        # ``overhead_tokens=None`` forever.
        #
        # Mirror ``streaming.py:_assemble_history_wire`` exactly: compacted
        # head (NOT recompressed) + the verbatim increment past the anchor.
        sent_wire = wire
        if (
            prior_checkpoint is not None
            and 0 <= prior_checkpoint.anchor_index <= len(raw_messages)
        ):
            sent_wire = [
                dict(m) for m in prior_checkpoint.compacted_wire
            ] + list(
                rebuild_history_wire_messages(
                    tuple(raw_messages)[prior_checkpoint.anchor_index:],
                ),
            )
        sent_wire_estimate = estimate_wire_tokens(
            sent_wire, model_hint=model_hint,
        )
        overhead_pre, overhead_source = compute_overhead_from_last_wire_measurement(
            conv=conv,
            wire_estimate_before=sent_wire_estimate,
            # ERA MATCHING (see the helper's docstring): the anchor must be
            # the one THIS compaction writes, so the selected assistant
            # measured the same wire generation ``sent_wire_estimate``
            # estimates. Passing ``prior_checkpoint.anchor_index`` here
            # selected a pre-compaction assistant and folded the compaction
            # GAIN into overhead, making the badge numerator go backwards.
            wire_anchor_index=new_anchor_index,
        )
        _log.info(
            "chat.compaction.overhead_estimate",
            conversation_id=conv.id.value,
            wire_estimate_before=sent_wire_estimate,
            overhead_tokens=overhead_pre,
            overhead_source=overhead_source,
        )
        new_ckpt = await self._compaction_engine.maybe_compress(
            checkpoint_key=checkpoint_key,
            assembled_wire=wire,
            history_messages_since_anchor=list(raw_messages)[prior_anchor:]
            if prior_anchor <= len(raw_messages)
            else [],
            anchor_index=new_anchor_index,
            anchor_message_id=anchor_message_id,
            completed_rounds=None,
            model_hint=model_hint,
            context_limit=budget,
            measured_eff_prompt=measured_eff_prompt,
            presend_eff_fallback=presend_eff_fallback,
            force=True,
            persist_id=conv.id,
            trigger_reason="force_compact",
            new_refs=_new_refs,
            overhead_tokens=overhead_pre,
        )

        # Task V follow-up (found by end-to-end testing): the streaming path
        # kicks the P2 session-digest + P3 turn-prefix summarisers right after
        # ``maybe_compress`` (``streaming.py:_compress_via_checkpoint``), but
        # the FORCE path did not — so a user-issued ``/compact`` only ever ran
        # the 4-phase drop and never produced an LLM summary.  Fire the same
        # two kicks here so both entry points behave identically.  Both are
        # fire-and-forget and gated on (a) the use case being wired and
        # (b) the model window clearing the small-window threshold, so an
        # unwired / small-window deployment stays byte-for-byte unchanged.
        if new_ckpt is not None:
            _digest_on = self._compaction_engine.digest_enabled(budget)
            _prefix_on = self._compaction_engine.turn_prefix_enabled(budget)
            _log.info(
                "chat.compact_forced.summariser_gates",
                conversation_id=conv.id.value,
                budget=budget,
                digest_enabled=_digest_on,
                turn_prefix_enabled=_prefix_on,
                digest_uc_wired=(
                    getattr(self._compaction_engine, "_refresh_digest_uc", None)
                    is not None
                ),
                turn_prefix_uc_wired=(
                    getattr(
                        self._compaction_engine,
                        "_summarize_turn_prefix_uc",
                        None,
                    )
                    is not None
                ),
            )
            if _digest_on:
                from qai.chat.application.use_cases.refresh_digest import (
                    RefreshDigestInput,
                )
                ledger_block: str | None = None
                if new_ckpt.reference_ledger is not None:
                    rendered = new_ckpt.reference_ledger.render_wire_block()
                    if rendered:
                        ledger_block = rendered
                self._compaction_engine.kick_digest_refresh(
                    checkpoint_key=checkpoint_key,
                    input=RefreshDigestInput(
                        conversation_id=conv.id,
                        checkpoint_key=checkpoint_key,
                        prev_digest=(
                            prior_checkpoint.digest_text
                            if prior_checkpoint is not None
                            else None
                        ),
                        dropped_wire=[dict(m) for m in wire],
                        reference_ledger_block=ledger_block,
                        model_id=model_hint,
                        context_window=budget,
                    ),
                )
            if _prefix_on:
                _prefix_wire, _user_msg_id = _compute_turn_prefix_at_boundary(
                    wire, context_limit=budget,
                )
                if _prefix_wire and _user_msg_id is not None:
                    from qai.chat.application.use_cases.summarize_turn_prefix import (
                        SummarizeTurnPrefixInput,
                    )
                    self._compaction_engine.kick_turn_prefix_summary(
                        checkpoint_key=checkpoint_key,
                        input=SummarizeTurnPrefixInput(
                            conversation_id=conv.id,
                            checkpoint_key=checkpoint_key,
                            user_message_id=_user_msg_id,
                            prefix_wire=_prefix_wire,
                            model_id=model_hint,
                            context_window=budget,
                        ),
                    )

        compacted = new_ckpt is not None
        if compacted:
            after_wire = [dict(m) for m in new_ckpt.compacted_wire]
            after_tokens = (
                estimate_wire_tokens(after_wire, model_hint=model_hint)
                + overhead_pre
            )
        else:
            # No new checkpoint. Two very different situations land here and
            # they must not report the same number:
            #
            #  * nothing is compacted at all (no standing checkpoint) — the
            #    raw wire IS what gets sent, so ``before_tokens`` is right;
            #  * compaction has CONVERGED — a checkpoint is already installed
            #    and the model keeps receiving its compacted wire. Reporting
            #    ``before_tokens`` here would tell the user "nothing more to
            #    reclaim (215.7K on the wire)" while the badge next to it
            #    truthfully shows 82.1K: the raw history is not the wire.
            #
            # Report what the model actually receives in both cases. The
            # standing checkpoint path folds the same ``overhead_pre`` so the
            # /compact reply matches the badge branch-Y read (which now also
            # includes overhead — engine change).
            standing = self._compaction_engine.get(checkpoint_key)
            if standing is not None:
                after_tokens = (
                    estimate_wire_tokens(
                        [dict(m) for m in standing.compacted_wire],
                        model_hint=model_hint,
                    )
                    + overhead_pre
                )
            else:
                after_tokens = before_tokens

        savings = max(0, before_tokens - after_tokens)
        ratio_after = (after_tokens / budget) if budget > 0 else 0.0
        usage = compute_context_usage(after_tokens, budget)
        _log.info(
            "chat.compact_forced",
            conversation_id=conv.id.value,
            budget=budget,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            savings=savings,
            compacted=compacted,
            ratio_after=usage.raw_ratio,
            overhead_tokens=overhead_pre,
            overhead_source=overhead_source,
        )

        escalation: Literal["ok", "suggest_handoff"] = "ok"
        if self._compaction_engine.should_suggest_handoff(checkpoint_key):
            escalation = "suggest_handoff"

        return ForceCompactChatResult(
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            savings=savings,
            budget_tokens=budget,
            ratio_after=ratio_after,
            escalation=escalation,
            compacted=compacted,
        )


__all__ = [
    "ForceCompactChatInput",
    "ForceCompactChatResult",
    "ForceCompactChatUseCase",
]
