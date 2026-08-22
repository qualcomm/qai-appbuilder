# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``GetContextDecisionUseCase`` -- read-only context-occupancy query.

Named ``compact.py`` for historical reasons: this file used to house the
``CompactChatUseCase`` that also mixed a (misleading) "trigger compaction"
identity with a pure read-only decision path. Task T retired the
compaction-trigger identity — the real compaction is driven by
:class:`~qai.chat.application.use_cases.force_compact.ForceCompactChatUseCase`
— and downgraded this use case to what it actually did all along: a
**read-only query** of a conversation's context occupancy, used by:

* ``GET /api/chat/conversations/{id}/context``  — the UI context badge;
* ``/compact status`` channel bridge command  — text status blurb.

No mutation is performed. Given a conversation id and the model's real
context-window in tokens (``budget_tokens``, resolved by the caller via
:func:`_resolve_context_window` / :func:`get_context_limit`), the use
case returns a :class:`ContextDecisionResult` describing:

* ``context_size`` — clamped ``used`` / ``budget`` / ``ratio``;
* ``needs_compaction`` — ``True`` when ``ratio >= INTER_ROUND_COMPRESS_THRESHOLD_RATIO``
  (the same dial the runtime compaction engine consults);
* ``raw_used_tokens`` / ``raw_ratio`` — un-clamped occupancy for the badge
  so an over-window state can be surfaced honestly;
* ``escalation`` — P5 handoff hint sourced from the shared compaction engine.

The mutation side (three-level summarisation, sliding-window selection,
chain-of-thought retention) lives in
:class:`~qai.chat.application.use_cases._compaction_engine.CompactionCheckpointEngine`
and is invoked either by :class:`StreamChatUseCase` on-turn or on-demand
via ``ForceCompactChatUseCase``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from dataclasses import dataclass

from qai.chat.application._token_estimate_helpers import (
    _last_assistant_with_usage,
    assistant_eff_prompt,
    coarse_byte_estimate,
    is_anthropic_family,
)
from qai.chat.application.ports import (
    ConversationRepositoryPort,
)
from qai.chat.application.use_cases._agentic_kernel import (
    INTER_ROUND_COMPRESS_THRESHOLD_RATIO,
)
from qai.chat.domain.content import (
    ContextSize,
    TokenCount,
    compute_context_usage,
)
from qai.chat.domain.errors import InvalidContextSizeError
from qai.chat.domain.ids import ConversationId
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.chat.application.use_cases._compaction_engine import (
        CompactionCheckpointEngine,
    )

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class GetContextDecisionInput:
    """Inputs for :meth:`GetContextDecisionUseCase.execute`.

    ``budget_tokens`` is the model's REAL context window (e.g. 200_000 for
    a 200K model). Caller is responsible for resolving it — the REST route
    uses :func:`_resolve_context_window`, the channel bridge / CLI use
    :func:`get_context_limit`. Passing a stale / fabricated value directly
    yields a misleading badge, so ONLY caller-side resolution is supported;
    the historical ``model_id`` shortcut has been removed to keep the
    contract explicit.
    """

    conversation_id: ConversationId
    budget_tokens: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextDecisionResult:
    """Result of :meth:`GetContextDecisionUseCase.execute`."""

    context_size: ContextSize
    needs_compaction: bool
    # The REAL (un-clamped) pre-compaction occupancy + ratio. ``context_size``
    # clamps ``used`` to ``budget`` to satisfy the ``ContextSize`` domain
    # invariant (``budget >= used``), which means it can never report an
    # over-window reading. These two carry the truth for the UI badge so it can
    # show e.g. "222K / 200K · 111%" once a history exceeds the model window
    # (the signal that compaction is imminent), instead of the misleading
    # "200K / 200K · 100%" floor. ``raw_ratio`` = ``raw_used_tokens / budget``
    # (un-clamped, so it may be > 1.0). Never persisted; computed live.
    raw_used_tokens: int
    raw_ratio: float
    # P5 (CONTEXT-COMPRESSION-NEXT v3.1 §7.2 / D11): handoff escape hatch
    # signal. ``"suggest_handoff"`` iff the current turn has fired ≥ 3
    # mid-turn compactions in a row AND a P2 session digest already exists
    # (see :meth:`CompactionCheckpointEngine.should_suggest_handoff`); the UI
    # then exposes a "migrate to a fresh conversation" entry. Any other state
    # (below threshold, no digest, no engine wired) reports ``"ok"``. Default
    # ``"ok"`` keeps the field HTTP-compatible — old clients that never read
    # it see the exact same payload they always did (AGENTS.md §3.1
    # tail-append). Never persisted; computed live from the engine.
    escalation: Literal["ok", "suggest_handoff"] = "ok"


class GetContextDecisionUseCase:
    """Read-only context-decision query.

    Returns the conversation's current occupancy + a ``needs_compaction``
    signal against the fixed engine threshold. Never mutates state and
    never triggers a compaction — for that use
    :class:`ForceCompactChatUseCase`.
    """

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        compaction_engine: "CompactionCheckpointEngine | None" = None,
    ) -> None:
        self._conversations = conversations
        # P5: optional engine reference so ``execute`` can read the current
        # mid-turn compaction counter + digest presence to derive the
        # ``escalation`` field. ``None`` (unit-test default / callers that
        # do not wire the streaming engine) keeps ``escalation="ok"``
        # unconditionally — byte-for-byte pre-P5 result.
        self._compaction_engine = compaction_engine

    async def execute(
        self, request: GetContextDecisionInput,
    ) -> ContextDecisionResult:
        if request.budget_tokens <= 0:
            raise InvalidContextSizeError(
                f"budget_tokens must be > 0, got {request.budget_tokens}",
            )
        budget = request.budget_tokens
        conv = await self._conversations.get(request.conversation_id)
        # Cloud-first ``used_tokens`` (replaces the local tiktoken BPE pass as
        # the PRIMARY source): prefer the per-conversation running counter
        # ``conv.full_history_tokens`` (migration 036) — the provider-measured
        # full (uncompressed) history wire size, maintained per turn in
        # ``StreamChatUseCase._finalize_assistant_message``. That is strictly
        # more accurate than re-tokenising, free (already persisted), and stays
        # correct across multi-round turns (it captures the LAST round's true
        # wire, not the cross-round prompt_tokens SUM).
        # Fallback chain when the counter is NULL (legacy / never measured):
        # derive once from the last assistant turn's ``usage.prompt_tokens``,
        # else a coarse bytes-based estimate over the history.
        # This is a READ-ONLY side path — ``conv.messages`` / the DB / the
        # display ``/messages`` route are NEVER touched.
        used_raw = conv.full_history_tokens
        # Track how ``used_raw`` was resolved (diagnostic — surfaced in the
        # ``chat.context_decision_evaluated`` log so we can confirm WHY the
        # badge shows a given size and whether the staleness兜底 fired).
        used_raw_source = "full_history_tokens"
        if used_raw is None:
            # Legacy / never-measured: derive once from last assistant, else
            # char estimate.
            last_asst = _last_assistant_with_usage(conv)
            if last_asst is not None:
                used_raw = int((last_asst.usage or {}).get("prompt_tokens") or 0)
                used_raw_source = "last_assistant_prompt_tokens"
            else:
                used_raw = (
                    coarse_byte_estimate(conv, None)
                    if conv.messages
                    else 0
                )
                used_raw_source = "coarse_byte_estimate"
        else:
            # State-Truth-First (AGENTS.md 🔴 铁律 1: 缓存脱节必须纠偏): the
            # counter ``full_history_tokens`` is the FAST path, but it can go
            # STALE — a turn interrupted mid-loop persists its assistant/tool
            # messages into ``conv`` (DB grows) and, on older builds, did NOT
            # advance the counter (the bug A fixes for new turns). For a
            # conversation whose counter was frozen by a PRE-fix interrupt, the
            # last assistant message still carries the provider's真实 measured
            # wire for that turn. When that measured size is significantly LARGER
            # than the counter, the counter is脱节 — trust the measurement
            # instead (take the max). This never DROPS the counter below the
            # real history;正常情况 (counter fresh / >= measured) keeps the
            # cloud-first counter口径 unchanged (judgement 2: no regression).
            last_asst = _last_assistant_with_usage(conv)
            if last_asst is not None:
                measured = assistant_eff_prompt(
                    last_asst.usage or {},
                    is_anthropic_family(getattr(last_asst, "model_id", None)),
                )
                # 10% slack: only treat as脱节 when the measurement clearly
                # exceeds the counter (avoid flapping on tiny per-turn deltas /
                # rounding between the eff_prompt口径 and the stored counter).
                if measured > int(used_raw) * 1.10 and measured > int(used_raw):
                    used_raw = measured
                    used_raw_source = "stale_counter_fallback_measured"
        # ``ContextSize.__post_init__`` enforces ``budget >= used``, so clamp
        # to ``budget`` (and sanity-clamp negatives) before constructing.
        # Shared口径 with the sub-agent badge via ``compute_context_usage``
        # (judgement 1: one calculation, two callers).
        usage = compute_context_usage(used_raw, budget)
        size = ContextSize(
            used=TokenCount(usage.used_clamped),
            budget=TokenCount(budget),
        )
        # Fixed engine threshold: the runtime compaction engine gates on this
        # value, so the badge signals compaction the moment the engine would.
        needs = size.is_over_threshold(INTER_ROUND_COMPRESS_THRESHOLD_RATIO)
        # REAL (un-clamped) occupancy for the UI badge — see ContextDecisionResult.
        # Negatives are still sanitised to 0, but values above ``budget`` are
        # preserved so the badge can surface an over-window state honestly.
        raw_used = usage.raw_used
        raw_ratio = usage.raw_ratio
        _log.info(
            "chat.context_decision_evaluated",
            conversation_id=conv.id.value,
            used=size.used.value,
            budget=size.budget.value,
            raw_used=raw_used,
            needs_compaction=needs,
            full_history_tokens_raw=conv.full_history_tokens,
            used_raw_resolved=used_raw,
            used_raw_source=used_raw_source,
            used_clamped=usage.used_clamped,
        )
        # P5: derive escalation from the engine (if wired). ``"ok"`` when no
        # engine is wired, when the mid-turn counter is < 3, or when the
        # current checkpoint has no digest — see
        # :meth:`CompactionCheckpointEngine.should_suggest_handoff` for the
        # gate.
        escalation: Literal["ok", "suggest_handoff"] = "ok"
        if self._compaction_engine is not None:
            if self._compaction_engine.should_suggest_handoff(
                request.conversation_id.value,
            ):
                escalation = "suggest_handoff"
        return ContextDecisionResult(
            context_size=size,
            needs_compaction=needs,
            raw_used_tokens=raw_used,
            raw_ratio=raw_ratio,
            escalation=escalation,
        )


__all__ = [
    "GetContextDecisionUseCase",
    "GetContextDecisionInput",
    "ContextDecisionResult",
]
