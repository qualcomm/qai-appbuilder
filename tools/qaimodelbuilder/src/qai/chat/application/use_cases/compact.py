# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``CompactChatUseCase`` -- decide whether a conversation needs compaction.

Compaction shrinks a long conversation history into a smaller summary
plus the most recent N messages so subsequent turns fit within the
model's context window.

This use case owns only the **decision** side: it validates the
request, reads the conversation's cloud-measured running token counter
(with a coarse char-based fallback), and returns a
:class:`CompactChatResult` describing whether compaction
is needed under the supplied threshold.  The mutation side (three-level
summarisation, sliding-window selection, chain-of-thought retention)
lives in :class:`~qai.chat.adapters.context_compressor.ThreeLevelContextCompressor`
and is invoked by :class:`StreamChatUseCase` when the decision returned
here flags ``needs_compaction``.

Splitting the decision and the mutation keeps the use case pure and
deterministic for unit tests, while letting the streaming pipeline
drive the heavier compression call only when warranted.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.chat.application._token_estimate_helpers import (
    _last_assistant_with_usage,
    assistant_eff_prompt,
    coarse_char_estimate,
    is_anthropic_family,
)
from qai.chat.application.ports import (
    ConversationRepositoryPort,
)
from qai.chat.domain.content import (
    ContextSize,
    TokenCount,
    compute_context_usage,
)
from qai.chat.domain.errors import InvalidContextSizeError
from qai.chat.domain.ids import ConversationId
from qai.chat.domain.model_profiles import get_context_limit
from qai.platform.logging import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class CompactChatInput:
    conversation_id: ConversationId
    budget_tokens: int
    trigger_threshold: float = 0.8
    # Optional model id (V1 parity: backend/token_counter.py model→limit
    # map).  When provided, the use case resolves the model's real
    # context-window via :func:`get_context_limit` and uses that as the
    # budget, overriding ``budget_tokens``.  When ``None`` the supplied
    # ``budget_tokens`` is honoured verbatim (full backwards compat).
    model_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CompactChatResult:
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


class CompactChatUseCase:
    """Decide whether a conversation needs compaction (no mutation yet)."""

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
    ) -> None:
        self._conversations = conversations

    async def execute(self, request: CompactChatInput) -> CompactChatResult:
        if request.budget_tokens <= 0:
            raise InvalidContextSizeError(
                f"budget_tokens must be > 0, got {request.budget_tokens}",
            )
        if not 0.0 <= request.trigger_threshold <= 1.0:
            raise InvalidContextSizeError(
                f"trigger_threshold must be in [0, 1], got "
                f"{request.trigger_threshold!r}",
            )
        # V1 parity: when a model id is supplied, resolve the model's real
        # context-window and use it as the budget (200K / 128K / 32K ...),
        # instead of the caller-supplied default.  ``get_context_limit``
        # always returns a positive int, so the > 0 invariant still holds.
        budget = request.budget_tokens
        if request.model_id:
            budget = get_context_limit(request.model_id)
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
        # else a coarse char-based estimate over the history.
        # This is a READ-ONLY side path — ``conv.messages`` / the DB / the
        # display ``/messages`` route are NEVER touched.
        used_raw = conv.full_history_tokens
        # Track how ``used_raw`` was resolved (diagnostic — surfaced in the
        # ``chat.compact_evaluated`` log so we can confirm WHY the badge shows a
        # given size and whether the staleness兜底 fired).
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
                    coarse_char_estimate(conv, request.model_id)
                    if conv.messages
                    else 0
                )
                used_raw_source = "coarse_char_estimate"
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
        needs = size.is_over_threshold(request.trigger_threshold)
        # REAL (un-clamped) occupancy for the UI badge — see CompactChatResult.
        # Negatives are still sanitised to 0, but values above ``budget`` are
        # preserved so the badge can surface an over-window state honestly.
        raw_used = usage.raw_used
        raw_ratio = usage.raw_ratio
        _log.info(
            "chat.compact_evaluated",
            conversation_id=conv.id.value,
            used=size.used.value,
            budget=size.budget.value,
            raw_used=raw_used,
            needs_compaction=needs,
            # DIAG (token-display investigation): show the badge-occupancy
            # source so we can confirm what /context returns to the frontend.
            # ``full_history_tokens`` is the persisted counter; when it is the
            # collapsed value (e.g. 15 after a cache-hit "hi") the badge shows
            # ~0.0K. Remove once root-caused.
            full_history_tokens_raw=conv.full_history_tokens,
            used_raw_resolved=used_raw,
            used_raw_source=used_raw_source,
            used_clamped=usage.used_clamped,
        )
        return CompactChatResult(
            context_size=size,
            needs_compaction=needs,
            raw_used_tokens=raw_used,
            raw_ratio=raw_ratio,
        )


__all__ = [
    "CompactChatUseCase",
    "CompactChatInput",
    "CompactChatResult",
]
