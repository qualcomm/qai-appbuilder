# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Conversation-agnostic context-compaction + checkpoint engine.

Extracted (verbatim behaviour) from :class:`StreamChatUseCase`'s inline
"差分 + checkpoint" logic so the SAME compaction algorithm can later be reused
by the sub-agent tool handler (``AgentToolHandler``) without dragging in the
``Conversation`` aggregate.

DESIGN (judgement 1 — architecture): this engine is a clean, single-
responsibility ``application``-layer component. It owns ONLY the compaction
decision + the per-conversation in-memory checkpoint cache + the optional
durable write-through store. Every ``conv`` coupling point that lived inside
``_compress_via_checkpoint`` is now a plain parameter (already-sliced
``history_messages_since_anchor``, pre-computed ``anchor_index`` /
``anchor_message_id`` / ``context_limit`` / ``presend_eff_fallback``), so the
engine has NO knowledge of ``Conversation``, ``conv.messages``, request shape,
or wire-assembly. The owning use case stays the thin conv-aware wrapper that
assembles the wire, derives the anchor, and fires the ``ON_TRUNCATE`` hook.

DESIGN (judgement 2 — no regression): the trigger gate (chars-floor vs
measured ``实发`` against ``threshold_ratio × context_limit``), the real-token
differential attribution, the compressor invocation (every argument forwarded
verbatim), the checkpoint storage口径, the durable persistence write-through,
and the ratio resolution/clamp are all byte-for-byte the same code paths that
ran inline before. The engine purposefully does NOT call the compressor with
any new argument, nor change any clamp/threshold constant.

Layering (AGENTS.md §3.2 / §3.5): imports only ``domain`` + same-level
``application`` helpers (``_agentic_kernel`` / ``_streaming_helpers``) +
``ports`` + ``platform`` logging. No ``adapters`` / ``infrastructure`` /
``interfaces`` / cross-context imports. ``completed_rounds`` is consumed by
DUCK-TYPING (``.real_prompt_tokens`` / ``.source`` via
``build_group_real_tokens``) so the engine does NOT depend on the
``_CompletedRound`` class living in ``streaming.py`` — that avoids an import
cycle (``streaming.py`` imports this engine).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import structlog

from qai.chat.application.use_cases._agentic_kernel import (
    COMPRESS_TARGET_WINDOW_RATIO as _DEFAULT_TARGET_WINDOW_RATIO,
    PROTECT_WINDOW_RATIO as _DEFAULT_PROTECT_WINDOW_RATIO,
    CompactionCheckpoint,
    estimate_wire_tokens as _estimate_wire_tokens,
)
from qai.chat.application.use_cases._streaming_helpers import (
    build_group_real_tokens as _build_group_real_tokens,
)

if TYPE_CHECKING:
    from qai.chat.application.ports import (
        CompactionCheckpointStorePort,
        ContextCompressionPort,
    )
    from qai.chat.domain.ids import ConversationId

_log = structlog.get_logger(__name__)


class CompactionCheckpointEngine:
    """Conversation-agnostic compaction + checkpoint engine.

    See module docstring. Holds the per-conversation in-memory checkpoint
    cache (keyed by an opaque ``checkpoint_key`` string the caller supplies,
    prefixed by ``key_prefix`` so several agents can share one engine instance
    without colliding) and, optionally, a durable write-through store keyed by
    :class:`ConversationId`.
    """

    def __init__(
        self,
        *,
        compressor: "ContextCompressionPort | None",
        ratio_provider: (
            Callable[[], Awaitable[dict[str, float]] | dict[str, float]] | None
        ) = None,
        checkpoint_store: "CompactionCheckpointStorePort | None" = None,
        threshold_ratio: float,
        target_ratio: float,
        preserve_tail: int,
        key_prefix: str = "",
    ) -> None:
        # In-memory write-through cache: checkpoint_key (already prefixed) →
        # ``CompactionCheckpoint``. Process-lifetime (the owning use case is a
        # process singleton). ``conv.messages`` is NEVER mutated by compaction
        # (dual-track history model) — this is the only compaction state.
        self._checkpoints: dict[str, CompactionCheckpoint] = {}
        # Conversations whose durable checkpoint has been lazy-loaded this
        # process (so the load is attempted at most once; a miss is memoised
        # as "loaded, none").
        self._loaded: set[str] = set()
        self._compressor = compressor
        self._ratio_provider = ratio_provider
        self._checkpoint_store = checkpoint_store
        self._threshold_ratio = threshold_ratio
        self._target_ratio = target_ratio
        self._preserve_tail = preserve_tail
        self._key_prefix = key_prefix

    # ------------------------------------------------------------------
    # Key helper
    # ------------------------------------------------------------------
    def _full_key(self, checkpoint_key: str) -> str:
        return f"{self._key_prefix}{checkpoint_key}"

    # ------------------------------------------------------------------
    # Cache access
    # ------------------------------------------------------------------
    def get(self, checkpoint_key: str) -> CompactionCheckpoint | None:
        """Return the in-memory checkpoint for ``checkpoint_key`` or ``None``."""
        return self._checkpoints.get(self._full_key(checkpoint_key))

    @property
    def checkpoints(self) -> dict[str, CompactionCheckpoint]:
        """The raw in-memory cache (callers may proxy-read it for compat)."""
        return self._checkpoints

    def is_loaded(self, checkpoint_key: str) -> bool:
        return self._full_key(checkpoint_key) in self._loaded

    def mark_loaded(self, checkpoint_key: str) -> None:
        self._loaded.add(self._full_key(checkpoint_key))

    def invalidate(self, checkpoint_key: str) -> bool:
        """Drop the in-memory checkpoint; return whether one existed.

        Also forgets the lazy-load memo so a subsequent turn re-loads the
        (possibly future-rewritten) persisted copy rather than trusting the
        now-dropped in-memory state. Idempotent.
        """
        key = self._full_key(checkpoint_key)
        existed = key in self._checkpoints
        if existed:
            self._checkpoints.pop(key, None)
        self._loaded.discard(key)
        return existed

    # ------------------------------------------------------------------
    # Durable persistence (best-effort; no-op without a store)
    # ------------------------------------------------------------------
    async def persist(
        self, persist_id: "ConversationId | None", ckpt: CompactionCheckpoint,
    ) -> None:
        """Write a checkpoint through to the durable store (best-effort)."""
        store = self._checkpoint_store
        if store is None or persist_id is None:
            return
        try:
            await store.save(persist_id, ckpt)
        except Exception as exc:  # noqa: BLE001 — never break the turn
            _log.warning(
                "chat.compaction.checkpoint_persist_failed",
                conversation_id=persist_id.value,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    async def drop_persisted(self, persist_id: "ConversationId") -> None:
        """Delete a checkpoint from the durable store (best-effort)."""
        store = self._checkpoint_store
        if store is None:
            return
        try:
            await store.delete(persist_id)
        except Exception as exc:  # noqa: BLE001 — never break the caller
            _log.warning(
                "chat.compaction.checkpoint_drop_failed",
                conversation_id=persist_id.value,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    async def lazy_load(
        self, checkpoint_key: str, persist_id: "ConversationId | None",
    ) -> None:
        """Populate the in-memory checkpoint for a conversation from sqlite (once).

        At most once per conversation per process (a miss is memoised). When
        the in-memory dict already holds a checkpoint (created this process,
        the steady-state case), the load is skipped entirely — the memory copy
        is newer/equal, never staler. No-op without a store / persist_id.
        """
        store = self._checkpoint_store
        if store is None:
            return
        key = self._full_key(checkpoint_key)
        if key in self._loaded:
            return
        if key in self._checkpoints:
            # Already have an in-process checkpoint (write-through created it);
            # do not overwrite it with a possibly-older persisted copy.
            self._loaded.add(key)
            return
        if persist_id is None:
            return
        try:
            loaded = await store.load(persist_id)
        except Exception as exc:  # noqa: BLE001 — never break the turn
            _log.warning(
                "chat.compaction.checkpoint_load_failed",
                conversation_id=persist_id.value,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return
        # Remember the attempt regardless of hit/miss so we never re-query.
        self._loaded.add(key)
        if loaded is not None and key not in self._checkpoints:
            self._checkpoints[key] = loaded
            _log.info(
                "chat.compaction.checkpoint_restored",
                conversation_id=persist_id.value,
                anchor=loaded.anchor_index,
                messages=len(loaded.compacted_wire),
            )

    # ------------------------------------------------------------------
    # Ratio resolution
    # ------------------------------------------------------------------
    async def resolve_ratios(self) -> dict[str, float]:
        """Resolve the user-chosen compaction ratios for this compaction.

        Reads the injected ``ratio_provider`` (sync or async) returning
        ``{"target": float, "protect": float}``. Each value is clamped to
        ``[0.01, 1.0]``; ``protect`` is additionally capped at ``target`` so
        the protected recent region can never exceed the post-compression
        target. Any missing key / malformed value / provider failure falls
        back to the kernel defaults (0.35 / 0.35), reproducing the prior
        behaviour byte-for-byte when no provider is wired.
        """
        target = _DEFAULT_TARGET_WINDOW_RATIO
        protect = _DEFAULT_PROTECT_WINDOW_RATIO
        provider = self._ratio_provider
        if provider is not None:
            try:
                result = provider()
                if isinstance(result, Awaitable):
                    result = await result
                if isinstance(result, dict):
                    target = self._clamp_ratio(
                        result.get("target"), _DEFAULT_TARGET_WINDOW_RATIO
                    )
                    protect = self._clamp_ratio(
                        result.get("protect"), _DEFAULT_PROTECT_WINDOW_RATIO
                    )
            except Exception as exc:  # noqa: BLE001 — pref read must never break
                _log.warning("chat.compaction.ratio_provider_failed", error=str(exc))
                target = _DEFAULT_TARGET_WINDOW_RATIO
                protect = _DEFAULT_PROTECT_WINDOW_RATIO
        # The protected recent region must not exceed the post-compression
        # target, else a pass cannot shrink the wire (protect floor >= target).
        if protect > target:
            protect = target
        return {"target": target, "protect": protect}

    @staticmethod
    def _clamp_ratio(value: Any, fallback: float) -> float:
        """Clamp a raw pref value to ``[0.01, 1.0]``; ``fallback`` if invalid."""
        try:
            if value is None:
                return fallback
            v = float(value)
        except (TypeError, ValueError):
            return fallback
        if v != v:  # NaN
            return fallback
        return max(0.01, min(1.0, v))

    # ------------------------------------------------------------------
    # Core compaction
    # ------------------------------------------------------------------
    async def maybe_compress(
        self,
        *,
        checkpoint_key: str,
        assembled_wire: list[dict[str, Any]],
        history_messages_since_anchor: list[Any],
        anchor_index: int,
        anchor_message_id: str | None,
        completed_rounds: list[Any] | None,
        model_hint: str,
        context_limit: int,
        measured_eff_prompt: int | None = None,
        presend_eff_fallback: int = 0,
        force: bool = False,
        live_wire_mode: bool = True,
        persist_id: "ConversationId | None" = None,
    ) -> CompactionCheckpoint | None:
        """Compress ``assembled_wire`` into a new checkpoint when triggered.

        Returns the NEW :class:`CompactionCheckpoint` (stored + persisted) when
        compaction fired; ``None`` on no-op (below preserve-tail / under the
        trigger threshold) or on compressor failure (best-effort — never
        raises). The caller is responsible for any conv-side follow-up (e.g.
        firing the ``ON_TRUNCATE`` hook, rebuilding its live wire).

        ``anchor_index`` / ``anchor_message_id`` are pre-computed by the caller
        (they depend on the live-wire-vs-history口径 + ``conv.messages``); the
        engine just stores them verbatim. ``history_messages_since_anchor`` is
        the already-sliced ``history[anchor:]`` used ONLY for the per-group
        real-token differential attribution.
        """
        if self._compressor is None:
            return None
        assembled = assembled_wire
        if len(assembled) <= self._preserve_tail:
            return None

        model_id = model_hint
        # Provider-measured REAL size of the wire being compressed (实发), used
        # by the compressor to derive this conversation's true token density.
        # Known on the non-force trigger path; ``None`` on the force /
        # prompt_too_long retry path → the compressor falls back to a fixed
        # chars/token factor.
        measured_wire_tokens: int | None = None
        if not force:
            # Trigger口径 = the REAL size actually being sent to the model
            # ("实发"), NOT the badge occupancy. State-Truth-First (铁律 1): the
            # provider's per-round ``last_round_prompt_tokens`` (+cache_read for
            # Anthropic) IS the measured size of the wire it just received. The
            # chars/4 estimate is kept only as a FLOOR so local models (usage
            # stays 0) still trigger on a genuinely huge wire.
            chars_estimate = _estimate_wire_tokens(
                assembled,
                model_hint=model_id,
            )
            if measured_eff_prompt is not None and measured_eff_prompt > 0:
                eff_send = int(measured_eff_prompt)
            else:
                eff_send = int(presend_eff_fallback)
            used = max(eff_send, chars_estimate)
            measured_wire_tokens = used if used > 0 else None
            threshold = int(context_limit * self._threshold_ratio)
            if used < threshold:
                return None
            _log.info(
                "chat.compaction.trigger",
                model_id=model_id,
                used_tokens=used,
                chars_estimate=chars_estimate,
                eff_send_tokens=eff_send,
                measured_eff_prompt=measured_eff_prompt,
                context_limit=context_limit,
                threshold=threshold,
                messages_before=len(assembled),
                anchor=anchor_index,
            )

        try:
            # Window-anchored compression: hand the model context window to the
            # compressor so it targets ``budget × ~0.35`` (and protects the most
            # recent ``PROTECT_WINDOW_RATIO`` verbatim). ``wire_actual_tokens``
            # feeds the compressor the conversation's REAL provider-measured
            # wire size so it derives a true token density (no tokenizer call).
            budget_tokens = context_limit
            # ── Real-token differential attribution (same-source, same-order) ──
            # Build a list of per-atomic-group prompt-token contributions that
            # is aligned BY CONSTRUCTION with the wire we just passed as
            # ``assembled`` (no fragile id() reverse lookup; see
            # docs/90-refactor/CONTEXT-COMPRESSION.md §"方案 R").
            # Best-effort: a failure here MUST NOT abort compression — fall
            # back to char × density inside the compressor.
            existing_ckpt = self._checkpoints.get(self._full_key(checkpoint_key))
            group_real_tokens: list[int] | None
            try:
                compacted_est = (
                    existing_ckpt.estimated_tokens
                    if existing_ckpt is not None
                    else None
                )
                group_real_tokens = _build_group_real_tokens(
                    assembled=assembled,
                    history_messages_since_anchor=history_messages_since_anchor,
                    compacted_estimated_tokens=compacted_est,
                    completed_rounds=(
                        list(completed_rounds)
                        if completed_rounds is not None
                        else None
                    ),
                    live_wire_mode=live_wire_mode,
                )
                _log.info(
                    "chat.compaction.real_token_attribution",
                    groups=len(group_real_tokens),
                    non_zero=sum(1 for v in group_real_tokens if v > 0),
                    live_wire_mode=live_wire_mode,
                    has_checkpoint=(existing_ckpt is not None),
                )
            except Exception as exc:  # noqa: BLE001 — never break compression
                _log.warning(
                    "chat.compaction.real_token_attribution_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                group_real_tokens = None
            ratios = await self.resolve_ratios()
            compressed = await self._compressor.compress(
                assembled,
                target_ratio=self._target_ratio,
                preserve_tail=self._preserve_tail,
                budget_tokens=budget_tokens,
                protect_ratio=ratios["protect"],
                wire_actual_tokens=measured_wire_tokens,
                group_real_tokens=group_real_tokens,
                target_window_ratio=ratios["target"],
                model_hint=model_id,
            )
        except Exception as exc:  # noqa: BLE001 — compression is best-effort
            _log.warning(
                "chat.compaction.failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

        # Stash a coarse char-based estimate of the compacted wire's prompt
        # tokens (cloud-first token accounting). ~4 chars/token over the
        # compacted message contents. Bootstrap value the badge returns until
        # the next real turn's provider-measured usage lands.
        est = sum(len(str(m.get("content") or "")) for m in compressed) // 4
        ckpt = CompactionCheckpoint(
            anchor_index=anchor_index,
            compacted_wire=[dict(m) for m in compressed],
            estimated_tokens=est or None,
            anchor_message_id=anchor_message_id,
        )
        full_key = self._full_key(checkpoint_key)
        self._checkpoints[full_key] = ckpt
        # Mark this conversation as "checkpoint-loaded" (the in-memory copy we
        # just created is now authoritative — no later lazy-load should
        # overwrite it) and WRITE THROUGH to the durable store so the checkpoint
        # survives a restart. Both are no-ops when no store is wired.
        self._loaded.add(full_key)
        await self.persist(persist_id, ckpt)
        _log.info(
            "chat.compaction.checkpoint_stored",
            anchor=anchor_index,
            messages_after=len(compressed),
            messages_before=len(assembled),
            chars_before=sum(
                len(str(m.get("content") or "")) for m in assembled
            ),
            chars_after=sum(
                len(str(m.get("content") or "")) for m in compressed
            ),
            est_compacted_tokens=est,
        )
        return ckpt
