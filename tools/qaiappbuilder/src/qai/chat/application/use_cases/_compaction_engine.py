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
``application`` helpers (``_agentic_kernel``) + ``ports`` + ``platform``
logging. No ``adapters`` / ``infrastructure`` / ``interfaces`` /
cross-context imports. ``streaming.py`` imports this engine, so the reverse
direction would create an import cycle — kept one-way.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

import structlog

from qai.chat.application.use_cases._agentic_kernel import (
    COMPRESS_TARGET_WINDOW_RATIO as _DEFAULT_TARGET_WINDOW_RATIO,
    PROTECT_WINDOW_RATIO as _DEFAULT_PROTECT_WINDOW_RATIO,
    CompactionCheckpoint,
    estimate_wire_tokens as _estimate_wire_tokens,
)
from qai.chat.domain.reference_ledger import ReferenceLedger

if TYPE_CHECKING:
    from qai.chat.application.ports import (
        CompactionCheckpointStorePort,
        CompactionJournalPort,
        ContextCompressionPort,
    )
    from qai.chat.domain.ids import ConversationId
    from qai.chat.domain.reference_ledger import Reference
    from qai.chat.application.use_cases.refresh_digest import (
        RefreshDigestInput,
        RefreshDigestUseCase,
    )
    from qai.chat.application.use_cases.summarize_turn_prefix import (
        SummarizeTurnPrefixInput,
        SummarizeTurnPrefixUseCase,
    )

_log = structlog.get_logger(__name__)


def _same_wire(
    a: "Sequence[dict[str, Any]]", b: "Sequence[dict[str, Any]]",
) -> bool:
    """True when two wires carry identical role/content/tool payloads.

    Used by the forced-compaction no-op guard to recognise "the compressor
    reproduced the checkpoint we already hold". Compares the fields that
    determine what the provider receives; ordering is significant.

    Cheap-exits on length so the common "genuinely different" case never pays
    for a full element walk.
    """
    if len(a) != len(b):
        return False
    for left, right in zip(a, b):
        if left.get("role") != right.get("role"):
            return False
        if left.get("content") != right.get("content"):
            return False
        if left.get("tool_calls") != right.get("tool_calls"):
            return False
        if left.get("tool_call_id") != right.get("tool_call_id"):
            return False
    return True


class CompactionCheckpointEngine:
    """Conversation-agnostic compaction + checkpoint engine.

    See module docstring. Holds the per-conversation in-memory checkpoint
    cache (keyed by an opaque ``checkpoint_key`` string the caller supplies,
    prefixed by ``key_prefix`` so several agents can share one engine instance
    without colliding) and, optionally, a durable write-through store keyed by
    :class:`ConversationId`.

    Threading contract (P2-2, single-loop assumption): every mutator on the
    kick-task registries (:meth:`kick_digest_refresh`,
    :meth:`kick_turn_prefix_summary`, :meth:`invalidate`) and the in-memory
    checkpoint dict (:meth:`maybe_compress`, :meth:`update_in_memory`,
    :meth:`invalidate`) assumes a SINGLE running asyncio event loop — the
    ``is``-check inside each ``_drop_when_done`` callback is the only guard
    against a stale registry pop, and it is safe only because two callers on
    the same loop cannot both observe "no prior task" in the same tick. Porting
    the engine to a multi-loop or multi-threaded setup MUST introduce
    ``asyncio.Lock`` around each registry op + the ``_checkpoints`` dict, or
    swap the dicts for a lock-free structure. Not required today.
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
        journal: "CompactionJournalPort | None" = None,
        refresh_digest_uc: "RefreshDigestUseCase | None" = None,
        summarize_turn_prefix_uc: "SummarizeTurnPrefixUseCase | None" = None,
        prune_redundant: bool = True,
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
        # ``key_prefix`` namespaces checkpoints when a single engine instance
        # is shared by multiple callers (e.g. main agent + sub-agent tool
        # handler; see module docstring lines 10-11). An empty prefix is legal
        # for the sole-caller case (main agent), but two callers sharing an
        # instance MUST supply distinct prefixes — otherwise same-named
        # ``checkpoint_key`` from different agents collide and overwrite each
        # other. We can't enforce uniqueness across independent constructions
        # here, but we DO record the prefix so future instrumentation / dev
        # asserts have a stable field to check.
        self._key_prefix = key_prefix
        # Best-effort per-compaction event sink (CONTEXT-COMPRESSION-NEXT
        # §5.1). When wired, every successful compaction emits one JSONL line
        # capturing trigger inputs (used/eff/threshold), before/after shape
        # (messages/chars), the phase the compressor reached, and the checkpoint
        # anchors. Fire-and-forget: the journal's ``append`` NEVER raises, and
        # we still hand it off via :func:`asyncio.create_task` so a future
        # journal implementation cannot regress into blocking the caller's
        # turn. ``None`` disables the emission (byte-for-byte prior behaviour).
        self._journal = journal
        # Phase 0 (redundant tool-group pruning) toggle forwarded to
        # ``ContextCompressionPort.compress``. Defaults ``True`` because that
        # is the correct main-agent behaviour and a future caller should
        # inherit the optimisation instead of silently regressing by
        # forgetting a kwarg.
        #
        # NOTE: the compressor's OWN ``prune_redundant`` default is ``False``.
        # The opposite directions are DELIBERATE: a bare port call must not
        # change shape (protecting direct-compressor callers/tests), while
        # the engine — the production entry point — opts in. The sub-agent
        # handler passes ``False`` explicitly to stay byte-equivalent. Do NOT
        # "harmonise" these two defaults.
        self._prune_redundant = prune_redundant
        # P2 (CONTEXT-COMPRESSION-NEXT §6): optional refresh-digest use case
        # + per-conversation task registry the ON_TRUNCATE caller fires
        # asynchronously. ``None`` disables the digest entirely — the wire-
        # assembly path then never sees ``ckpt.digest_text`` populated and
        # is byte-for-byte identical to pre-P2 behaviour. When wired,
        # :meth:`kick_digest_refresh` starts a background ``asyncio.Task``
        # per checkpoint_key; the SAME key firing again cancels the prior
        # task first (D8: latest kick wins — the newer inputs are strictly
        # more recent, so the older run is stale by construction).
        self._refresh_digest_uc = refresh_digest_uc
        self._digest_refresh_tasks: dict[str, asyncio.Task[None]] = {}
        # P3 (CONTEXT-COMPRESSION-NEXT §7.1): optional turn-prefix summariser
        # + its OWN per-conversation task registry. Kept in a distinct dict
        # from ``_digest_refresh_tasks`` so P2 and P3 kicks for the same
        # conversation can run concurrently without cancelling each other —
        # the two paths write to DIFFERENT columns on the checkpoint row and
        # semantically cover different axes (P2 = cross-turn history, P3 =
        # single long-turn prefix). Within P3 itself the same "latest kick
        # wins" contract applies: :meth:`kick_turn_prefix_summary` cancels an
        # in-flight task for the same key before starting the fresh one.
        # ``None`` disables the summariser entirely — the wire-assembly path
        # then never sees ``ckpt.turn_prefix_summaries_json`` populated and
        # is byte-for-byte identical to pre-P3 behaviour.
        self._summarize_turn_prefix_uc = summarize_turn_prefix_uc
        self._turn_prefix_tasks: dict[str, asyncio.Task[None]] = {}
        # P5 (CONTEXT-COMPRESSION-NEXT v3.1 §7.2 / D11): per-conversation counter
        # of mid-turn compactions observed within a SINGLE turn. Incremented by
        # the streaming use case each time a mid-turn (in-flight follow-up loop)
        # compaction fires and reset when a new turn begins / a compaction lands
        # BELOW the threshold. ``should_suggest_handoff`` reads it together with
        # the current checkpoint's ``digest_text`` to gate the P5 handoff escape
        # hatch: three mid-turn compactions in one turn signal the turn is not
        # converging inside the window, and only THEN does the UI expose the
        # "migrate to new conversation" entry — provided a digest already
        # exists so the migration has something to carry over. No digest ⇒ the
        # UI hides the entry (``escalation`` stays ``"ok"``); ``None`` /
        # missing key ⇒ zero.
        self._consecutive_mid_turn_compacts: dict[str, int] = {}
        # P0-1 (Task O): install the in-memory-refresh callback on the P2/P3
        # use cases. Both persist their refreshed digest / turn-prefix
        # summary to the durable store, but neither owns the in-memory
        # checkpoint dict this engine holds — without a callback the engine
        # would keep serving a stale checkpoint (``digest_text``/
        # ``turn_prefix_summaries_json`` = ``None``) until a restart re-
        # hydrates from disk. Only assign when the caller has not already
        # wired one (respect an explicit override supplied at UC
        # construction time — the last writer wins is the composition
        # Task R: wire the FRAGMENT-scoped callbacks. Each background UC
        # persists only ITS own fragment (P2 = digest, P3 = turn-prefix)
        # and carries the ``core_generation`` it observed. The engine's
        # ``replace_*_fragment`` methods check the incoming generation
        # against the currently-cached frozen checkpoint's generation —
        # a mismatch is a stale write and the update is refused, so a
        # late P2/P3 completion can NEVER resurrect a checkpoint that
        # ``/compact clear`` (or a fresh compaction) has already
        # replaced. Only assign when the caller has not already wired
        # one (explicit override wins — the composition root's
        # responsibility, not the engine's).
        if (
            self._refresh_digest_uc is not None
            and getattr(self._refresh_digest_uc, "_on_persisted", None) is None
        ):
            self._refresh_digest_uc._on_persisted = self.replace_digest_fragment
        if (
            self._summarize_turn_prefix_uc is not None
            and getattr(self._summarize_turn_prefix_uc, "_on_persisted", None)
            is None
        ):
            self._summarize_turn_prefix_uc._on_persisted = (
                self.replace_turn_prefix_fragment
            )

    # ------------------------------------------------------------------
    # Key helper
    # ------------------------------------------------------------------
    def _full_key(self, checkpoint_key: str) -> str:
        return f"{self._key_prefix}{checkpoint_key}"

    # ------------------------------------------------------------------
    # P5 handoff-escape-hatch counter (CONTEXT-COMPRESSION-NEXT v3.1 §7.2)
    # ------------------------------------------------------------------
    # Three tiny mutators + one read. Kept next to ``_full_key`` because they
    # are pure key-scoped bookkeeping (no compressor / no store / no async).
    # The trigger is "≥ 3 mid-turn compactions in the SAME turn", i.e. the
    # turn keeps re-tripping the threshold instead of converging inside the
    # window. When that happens AND the current checkpoint already carries a
    # P2 digest (``digest_text`` non-empty), the UI SHOULD offer a "migrate
    # to a fresh conversation" entry (P5 escape hatch): the counter alone is
    # not enough — without a digest the migration has nothing to carry
    # forward, so the escalation stays ``"ok"``.
    def get_consecutive_mid_turn_compacts(self, checkpoint_key: str) -> int:
        """Return the current mid-turn compaction count for this conv (or 0)."""
        return self._consecutive_mid_turn_compacts.get(
            self._full_key(checkpoint_key), 0,
        )

    def increment_consecutive_mid_turn(self, checkpoint_key: str) -> int:
        """Bump the mid-turn counter by 1 and return the new value."""
        full = self._full_key(checkpoint_key)
        count = self._consecutive_mid_turn_compacts.get(full, 0) + 1
        self._consecutive_mid_turn_compacts[full] = count
        return count

    def reset_consecutive_mid_turn(self, checkpoint_key: str) -> None:
        """Clear the mid-turn counter (a new turn started / turn converged)."""
        self._consecutive_mid_turn_compacts.pop(
            self._full_key(checkpoint_key), None,
        )

    def should_suggest_handoff(self, checkpoint_key: str) -> bool:
        """True iff mid-turn count ≥ 3 AND the checkpoint has a digest.

        Two gates (v3.1 §7.2 / D11): (1) the turn tripped the compaction
        threshold at least three times without converging — a signal the
        conversation genuinely does not fit anymore; (2) a P2 session digest
        already exists so the migration has structured context to carry into
        the fresh conversation. Missing either gate keeps the escalation at
        ``"ok"``. Byte-for-byte HTTP compat: default response
        ``escalation="ok"`` is what old clients see (they ignore the field
        entirely).
        """
        full = self._full_key(checkpoint_key)
        if self._consecutive_mid_turn_compacts.get(full, 0) < 3:
            return False
        ckpt = self._checkpoints.get(full)
        return ckpt is not None and bool(ckpt.digest_text)

    # ------------------------------------------------------------------
    # Cache access
    # ------------------------------------------------------------------
    def get(self, checkpoint_key: str) -> CompactionCheckpoint | None:
        """Return the in-memory checkpoint for ``checkpoint_key`` or ``None``."""
        return self._checkpoints.get(self._full_key(checkpoint_key))

    def get_checkpoint(
        self, checkpoint_key: str,
    ) -> CompactionCheckpoint | None:
        """Public alias of :meth:`get` for callers that read the ledger.

        ``streaming.py`` uses this to look up the checkpoint's
        :attr:`~qai.chat.application.use_cases._agentic_kernel.CompactionCheckpoint.reference_ledger`
        when assembling the base wire (P1.d). Kept as a distinct symbol so
        the wire-assembly reader has a stable, non-private entry point,
        while the in-process compaction paths keep using :meth:`get` (no
        renames required at existing call sites).
        """
        return self.get(checkpoint_key)

    @property
    def checkpoints(self) -> dict[str, CompactionCheckpoint]:
        """The raw in-memory cache (callers may proxy-read it for compat)."""
        return self._checkpoints

    def digest_refresh_in_flight(self, checkpoint_key: str) -> bool:
        """Whether a P2 session-digest refresh is still running for this key.

        The digest is generated by a background ``asyncio.Task`` that calls the
        LLM, which takes 20-40s on a long history (and grows with it). The
        ``/compact`` reply returns as soon as the 4-phase drop finishes — long
        before the summary lands — so a user who immediately runs
        ``/compact migrate`` (which WANTS that summary) has no way to know it
        is still being written.

        Exposed through ``GET /context`` so the composer badge can show a
        "summarising" indicator for exactly as long as the task runs. Returns
        ``False`` when no task was ever kicked, when it already completed, or
        when it was cancelled.
        """
        return self.digest_refresh_task(checkpoint_key) is not None

    def digest_refresh_task(
        self, checkpoint_key: str,
    ) -> "asyncio.Task[None] | None":
        """The in-flight P2 digest task for this key, or ``None``.

        Companion to :meth:`digest_refresh_in_flight` for callers that need to
        AWAIT the task rather than just probe it (the P5 handoff waits for a
        pending summary before composing its document). Applies the engine's
        own ``key_prefix``, so callers pass the SAME un-prefixed key they give
        :meth:`maybe_compress` — reaching into ``_digest_refresh_tasks``
        directly would miss every entry under a prefixed (multi-agent) engine.

        Returns ``None`` when no task was kicked or the last one already
        settled; a settled task is deliberately still returned as ``None`` so
        the caller never awaits something that cannot make progress.
        """
        task = self._digest_refresh_tasks.get(self._full_key(checkpoint_key))
        if task is None or task.done():
            return None
        return task

    def is_loaded(self, checkpoint_key: str) -> bool:
        return self._full_key(checkpoint_key) in self._loaded

    def mark_loaded(self, checkpoint_key: str) -> None:
        self._loaded.add(self._full_key(checkpoint_key))

    def invalidate(self, checkpoint_key: str) -> bool:
        """Drop the in-memory checkpoint; return whether one existed.

        Also forgets the lazy-load memo so a subsequent turn re-loads the
        (possibly future-rewritten) persisted copy rather than trusting the
        now-dropped in-memory state. Idempotent.

        P0-2 (Task L): additionally cancels any in-flight fire-and-forget
        digest-refresh (P2) or turn-prefix-summary (P3) tasks pending on the
        SAME key. Without this, ``/compact clear`` would drop the checkpoint
        yet let a background summariser (started by the prior turn) finish
        seconds later and ``persist(..., ckpt_with_digest)`` — resurrecting a
        cleared digest and violating the "drop the compaction checkpoint"
        contract. Cancellation is safe because both use cases treat
        ``CancelledError`` as "abandon this run, do NOT persist stale text".
        """
        key = self._full_key(checkpoint_key)
        existed = key in self._checkpoints
        if existed:
            self._checkpoints.pop(key, None)
        self._loaded.discard(key)
        # Cancel any in-flight P2/P3 tasks scoped to this key so a late
        # completion cannot re-populate the checkpoint we just dropped.
        for reg in (self._digest_refresh_tasks, self._turn_prefix_tasks):
            task = reg.pop(key, None)
            if task is not None and not task.done():
                task.cancel()
        return existed

    def replace_digest_fragment(
        self,
        checkpoint_key: str,
        digest_text: str,
        digest_updated_at: str,
        core_generation: int,
    ) -> bool:
        """Task R: swap the digest projection on a frozen checkpoint (CAS).

        Called from :class:`RefreshDigestUseCase._execute_inner` after
        ``store.save_digest`` succeeds. The frozen dataclass means we
        MUST rebuild the checkpoint via :func:`dataclasses.replace`
        rather than mutate the fields in place — the swap is a single
        dict assignment so no reader can observe a torn state.

        The update is refused (``False``) when either:

        * the in-memory cache no longer holds a checkpoint for this key
          (a peer ``invalidate`` dropped it — Task P CRITICAL-4 root
          cause is now structurally impossible), or
        * the cached checkpoint's ``generation`` differs from
          ``core_generation`` (a peer ``maybe_compress`` advanced the
          core past the writer's snapshot — Task P CRITICAL-2 & HIGH-3
          root cause is now structurally impossible).
        """
        full_key = self._full_key(checkpoint_key)
        current = self._checkpoints.get(full_key)
        if current is None:
            _log.debug(
                "chat.compaction.fragment_write_stale_no_cache",
                checkpoint_key=full_key,
                incoming_core_generation=int(core_generation),
                reason="cache_dropped",
            )
            return False
        if int(current.generation) != int(core_generation):
            _log.debug(
                "chat.compaction.fragment_write_stale_generation",
                checkpoint_key=full_key,
                cached_generation=int(current.generation),
                incoming_core_generation=int(core_generation),
                fragment="digest",
            )
            return False
        self._checkpoints[full_key] = dataclasses.replace(
            current,
            digest_text=digest_text,
            digest_updated_at=digest_updated_at,
        )
        self._loaded.add(full_key)
        return True

    def replace_turn_prefix_fragment(
        self,
        checkpoint_key: str,
        summaries_json: str,
        core_generation: int,
    ) -> bool:
        """Task R: swap the turn-prefix projection on a frozen checkpoint (CAS).

        Same contract as :meth:`replace_digest_fragment`. Returns
        ``True`` on a successful in-memory swap, ``False`` when the
        cache no longer matches the writer's generation snapshot (peer
        invalidate or peer compaction wins — the DB CAS refused the
        write in the same conditions, so in-memory and durable state
        stay consistent).
        """
        full_key = self._full_key(checkpoint_key)
        current = self._checkpoints.get(full_key)
        if current is None:
            _log.debug(
                "chat.compaction.fragment_write_stale_no_cache",
                checkpoint_key=full_key,
                incoming_core_generation=int(core_generation),
                reason="cache_dropped",
            )
            return False
        if int(current.generation) != int(core_generation):
            _log.debug(
                "chat.compaction.fragment_write_stale_generation",
                checkpoint_key=full_key,
                cached_generation=int(current.generation),
                incoming_core_generation=int(core_generation),
                fragment="turn_prefix",
            )
            return False
        self._checkpoints[full_key] = dataclasses.replace(
            current,
            turn_prefix_summaries_json=summaries_json,
        )
        self._loaded.add(full_key)
        return True

    def replace_last_eff_prompt(
        self, checkpoint_key: str, last_eff_prompt: int,
    ) -> bool:
        """Task R: swap the ``last_eff_prompt`` slot on a frozen checkpoint.

        The TPP-1 delta-baseline field is the ONLY checkpoint slot mutated
        outside a compaction (streaming's per-turn full-history counter).
        With frozen dataclasses that mutation has to be expressed as a
        rebuild; the new instance keeps the SAME ``generation`` — this is
        a pure per-turn bookkeeping refresh, not a state advance, so the
        DB CAS still protects the row from a concurrent compaction that
        genuinely advances state.
        """
        full_key = self._full_key(checkpoint_key)
        current = self._checkpoints.get(full_key)
        if current is None:
            return False
        self._checkpoints[full_key] = dataclasses.replace(
            current, last_eff_prompt=int(last_eff_prompt),
        )
        return True

    def update_in_memory(
        self, checkpoint_key: str, ckpt: CompactionCheckpoint,
    ) -> None:
        """Low-level cache seed / replace helper (test + lazy-load hook).

        Kept for test setup and hand-off paths that need to install a
        specific frozen checkpoint instance without going through
        :meth:`maybe_compress` (e.g. lazy-loading a rehydrated row after
        a restart, or seeding a test's engine to a known state). The
        production P2/P3 async paths do NOT call this — they go through
        :meth:`replace_digest_fragment` /
        :meth:`replace_turn_prefix_fragment`, which enforce
        generation-CAS. This helper unconditionally replaces the cache
        entry, so it MUST NOT be wired as an ``on_persisted`` callback.
        """
        full_key = self._full_key(checkpoint_key)
        self._checkpoints[full_key] = ckpt
        self._loaded.add(full_key)

    # ------------------------------------------------------------------
    # Digest refresh (P2 — CONTEXT-COMPRESSION-NEXT §6)
    # ------------------------------------------------------------------
    def digest_enabled(self, context_window: int) -> bool:
        """Return whether asynchronous digest refresh is active for this window.

        Two gates (§6, D6): (1) a use case is wired at construction; (2) the
        model's context window is at least :data:`SMALL_WINDOW_THRESHOLD`.
        Small windows skip the digest because the ledger + verbatim increment
        already fit and running a summariser inside a tiny budget would
        thrash it. When either gate is closed the wire-assembly path never
        sees ``ckpt.digest_text`` populated — byte-for-byte pre-P2 wire.
        """
        from qai.chat.application.use_cases.refresh_digest import (
            SMALL_WINDOW_THRESHOLD,
        )
        if self._refresh_digest_uc is None:
            return False
        return context_window >= SMALL_WINDOW_THRESHOLD

    def kick_digest_refresh(
        self,
        *,
        checkpoint_key: str,
        input: "RefreshDigestInput",
    ) -> None:
        """Start a fire-and-forget digest refresh; cancel any prior task.

        D8: two rapid compactions on the same conversation MUST NOT interleave
        their digest writes — the newer inputs cover a strictly wider dropped
        history, so we cancel the older task and start a fresh one. The
        cancelled task's use case lets :class:`asyncio.CancelledError`
        propagate on purpose so it does not persist stale text (see
        :class:`RefreshDigestUseCase._execute_inner`).

        No hard timeout: the LLM stream's own abort registry / provider
        timeouts govern; here we only support cancellation via a NEXT kick.
        A missing running event loop (sync test harness) is degraded to a
        debug log — never raise into the caller's compaction hot path.

        ``checkpoint_key`` is un-prefixed (the SAME string the caller passes
        to :meth:`maybe_compress` / :meth:`get`); this method applies the
        engine's own ``key_prefix`` so tasks from different agents sharing
        one engine instance do not collide in the registry.
        """
        if self._refresh_digest_uc is None:
            return
        full_key = self._full_key(checkpoint_key)
        prev = self._digest_refresh_tasks.get(full_key)
        if prev is not None and not prev.done():
            prev.cancel()
        try:
            task = asyncio.create_task(
                self._refresh_digest_uc.execute(input),
                name=f"digest-refresh-{full_key[:16]}",
            )
        except RuntimeError:
            _log.debug(
                "chat.compaction.digest_kick_no_loop",
                checkpoint_key=full_key,
            )
            return
        self._digest_refresh_tasks[full_key] = task
        _log.info(
            "chat.compaction.digest_kick_started",
            checkpoint_key=full_key,
            task_name=task.get_name(),
        )
        # Remove the finished task from the registry so the dict does not
        # accumulate ``done`` entries across a long-running process. Uses a
        # default-value lambda so a late-firing callback does not pop a
        # NEWER task that landed under the same key after this one finished.
        def _drop_when_done(t: "asyncio.Task[None]", *, k: str = full_key) -> None:
            current = self._digest_refresh_tasks.get(k)
            if current is t:
                self._digest_refresh_tasks.pop(k, None)
        task.add_done_callback(_drop_when_done)

    # ------------------------------------------------------------------
    # Turn-prefix summary (P3 — CONTEXT-COMPRESSION-NEXT §7.1)
    # ------------------------------------------------------------------
    def turn_prefix_enabled(self, context_window: int) -> bool:
        """Return whether the async turn-prefix summariser is active.

        Two gates (matching :meth:`digest_enabled`): (1) a use case is wired
        at construction; (2) the model's context window is at least the
        small-window threshold. Both fail-closed to a no-op preserving pre-P3
        wire bytes.
        """
        # Local import to avoid cycles at module load (the use case module
        # imports ``SMALL_WINDOW_THRESHOLD`` from ``refresh_digest``, which
        # in turn imports from ``ports``; the engine is loaded early).
        from qai.chat.application.use_cases.refresh_digest import (
            SMALL_WINDOW_THRESHOLD,
        )
        if self._summarize_turn_prefix_uc is None:
            return False
        return context_window >= SMALL_WINDOW_THRESHOLD

    def kick_turn_prefix_summary(
        self,
        *,
        checkpoint_key: str,
        input: "SummarizeTurnPrefixInput",
    ) -> None:
        """Start a fire-and-forget turn-prefix summariser; cancel prior task.

        Kept in a distinct task registry from :meth:`kick_digest_refresh` so
        the P2 (session digest) and P3 (turn-prefix) paths for the SAME
        conversation may run concurrently — they write to different columns
        and cover semantically different axes. Within the P3 registry the
        "latest kick wins" contract still applies: a fresh kick against a
        key with an unfinished task cancels the prior one first (a rapid
        follow-up turn always covers a strictly newer prefix, so the older
        run is stale by construction).

        No hard timeout: the LLM stream's own abort registry / provider
        timeouts govern; here we only support cancellation via a NEXT kick.
        A missing running event loop (sync test harness) is degraded to a
        debug log — never raise into the caller's compaction hot path.
        """
        if self._summarize_turn_prefix_uc is None:
            return
        full_key = self._full_key(checkpoint_key)
        prev = self._turn_prefix_tasks.get(full_key)
        if prev is not None and not prev.done():
            prev.cancel()
        try:
            task = asyncio.create_task(
                self._summarize_turn_prefix_uc.execute(input),
                name=f"turn-prefix-{full_key[:16]}",
            )
        except RuntimeError:
            _log.debug(
                "chat.compaction.turn_prefix_kick_no_loop",
                checkpoint_key=full_key,
            )
            return
        self._turn_prefix_tasks[full_key] = task
        def _drop_when_done(t: "asyncio.Task[None]", *, k: str = full_key) -> None:
            current = self._turn_prefix_tasks.get(k)
            if current is t:
                self._turn_prefix_tasks.pop(k, None)
        task.add_done_callback(_drop_when_done)

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
            _log.warning(
                "chat.compaction.protect_gt_target_clamped",
                requested_protect=protect,
                target=target,
                message=(
                    "protect ratio > target ratio; clamping protect down to "
                    "target so a compaction pass can actually shrink the wire."
                    " Check the user's forge.config chat.compaction_* values."
                ),
            )
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
        new_refs: "list[Reference] | None" = None,
        trigger_reason: str = "unknown",
        overhead_tokens: int = 0,
        target_window_ratio_override: float | None = None,
    ) -> CompactionCheckpoint | None:
        """Compress ``assembled_wire`` into a new checkpoint when triggered.

        Returns the NEW :class:`CompactionCheckpoint` (stored + persisted) when
        compaction fired; ``None`` on no-op (below preserve-tail / under the
        trigger threshold) or on compressor failure (best-effort — never
        raises). The caller is responsible for any conv-side follow-up (e.g.
        firing the ``ON_TRUNCATE`` hook, rebuilding its live wire).

        ``anchor_index`` / ``anchor_message_id`` are pre-computed by the caller
        (they depend on the live-wire-vs-history口径 + ``conv.messages``); the
        engine just stores them verbatim. ``history_messages_since_anchor``,
        ``completed_rounds`` and ``live_wire_mode`` are accepted for
        signature stability with the caller and are currently ignored (the
        v3 drop-only compressor derives every ratio from the wire).

        ``overhead_tokens`` (badge口径 fold-in): number of tokens that the
        *runtime* wire carries in addition to ``compacted_wire`` — system
        prompt, tool schemas, persona, skill instructions. The engine has no
        access to those runtime blocks (they are assembled per-turn by the
        streaming path / ``force_compact`` caller), so the caller reverse-
        computes ``overhead_pre`` from the last provider-measured
        ``last_round_prompt_tokens`` and threads it in here. The engine folds
        this constant into the stashed ``estimated_tokens`` so the badge's
        bootstrap number (branch Y of ``estimate_compacted_tokens``) already
        matches the provider口径 the very next turn will report (branch X),
        eliminating the 76%→56% jitter window (docs §八 / §8.7 / §8.11).
        Default ``0`` keeps sub-agent / test paths byte-equivalent.

        ``target_window_ratio_override`` (context-overflow recovery): replaces
        the resolved ``ratios["target"]`` for THIS pass only. The recovery
        path (a provider rejected the wire for exceeding its context window)
        must aim strictly lower than the target that already failed, and the
        user's stored preference is exactly that failed target — so the caller
        passes a scaled-down ratio here instead of mutating the preference.
        ``protect`` is clamped down alongside it (the protected region can
        never exceed the compression target, or no pass could shrink the
        wire). ``None`` (the default) keeps every existing caller on the
        resolved user preference, byte-for-byte.
        """
        if self._compressor is None:
            return None
        assembled = assembled_wire
        if len(assembled) <= self._preserve_tail:
            return None

        model_id = model_hint
        # Journal fields (CONTEXT-COMPRESSION-NEXT §5.1). Initialised here so
        # they cover BOTH the non-force trigger branch (populated below) and
        # the force path (kept ``None`` — the retry doesn't gate on a size).
        bytes_estimate: int | None = None
        eff_send: int | None = None
        used: int | None = None
        threshold: int | None = None
        # Anchor of the checkpoint we're about to overwrite (or ``None`` when
        # this is the first compaction for the key). Captured here — the
        # write-through at ``self._checkpoints[full_key] = ckpt`` below drops
        # this reference; recording it up-front keeps the journal accurate.
        prev_anchor: int | None = None
        prev_ckpt = self._checkpoints.get(self._full_key(checkpoint_key))
        if prev_ckpt is not None:
            prev_anchor = prev_ckpt.anchor_index
        # Provider-measured REAL size of the wire being compressed (实发), used
        # by the compressor to derive this conversation's true token density.
        # Known on the non-force trigger path; ``None`` on the force /
        # prompt_too_long retry path → the compressor falls back to a fixed
        # bytes/token factor.
        measured_wire_tokens: int | None = None
        if not force:
            # Trigger口径 = the REAL size actually being sent to the model
            # ("实发"), NOT the badge occupancy. State-Truth-First (铁律 1): the
            # provider's per-round ``last_round_prompt_tokens`` (+cache_read for
            # Anthropic) IS the measured size of the wire it just received. The
            # bytes/4 estimate is kept only as a FLOOR so local models (usage
            # stays 0) still trigger on a genuinely huge wire.
            bytes_estimate = _estimate_wire_tokens(
                assembled,
                model_hint=model_id,
            )
            if measured_eff_prompt is not None and measured_eff_prompt > 0:
                eff_send = int(measured_eff_prompt)
            else:
                eff_send = int(presend_eff_fallback)
            # OVERHEAD-AWARE MAX (audit §3.6 fix): the two candidate
            # signals live in DIFFERENT口径 — mixing them naively was the
            # source of the pre-fix double-count.
            #
            #   * ``eff_send`` — provider-measured wire prompt tokens
            #     (mid-turn ``measured_eff_prompt``, i.e. the round that
            #     JUST completed on this turn; OR ``presend_eff_fallback``
            #     coming from ``_presend_eff_estimate`` under the
            #     non-degraded first_round branch). BOTH of these are
            #     ROUND-0-STYLE FULL WIRE measurements — the provider
            #     saw the system prompt + tool schemas + persona + skill
            #     blocks + assembly, and reported the sum. Overhead is
            #     ALREADY inside this number.
            #
            #   * ``bytes_estimate`` — the engine's local BPE (or bytes/4
            #     fallback) of the assembly BODY ONLY. The engine has no
            #     visibility into the runtime overhead blocks; they are
            #     assembled per-turn by the streaming path outside the
            #     compaction engine's scope. To make this comparable to
            #     ``eff_send`` we ADD ``overhead_tokens`` (reverse-
            #     computed by the caller via
            #     ``compute_overhead_from_last_wire_measurement``) once
            #     — and once only — HERE.
            #
            # Both terms of ``max`` now describe the SAME quantity:
            # predicted full send volume including overhead. No path can
            # count overhead twice.
            #
            # HISTORICAL BUG (pre-audit §3.6): the previous code was
            # ``used = max(eff_send, bytes_estimate)`` then
            # ``used_with_overhead = used + overhead_tokens``. When
            # ``eff_send`` won (mid-turn provider measurement, or the
            # D-final first_round predictor), overhead was inside
            # ``eff_send`` AND added again by the caller — a systematic
            # 40-55K over-count that made the trigger fire ~1 compaction
            # early on every turn with a non-zero overhead reading.
            #
            # DEGRADED PATH: ``_presend_eff_estimate`` returns 0 when the
            # only available usage row is a legacy pre-2026-07 row (no
            # ``first_round_prompt_tokens``, i.e. ``ProviderReading.
            # degraded=True`` — audit §3.5). ``eff_send`` = 0 collapses
            # the max to ``bytes_estimate + overhead_tokens`` — the
            # uncontaminated bytes path — instead of feeding the poisoned
            # ``last_round_prompt_tokens`` into the gate. ``max`` cannot
            # pull a large poisoned value down; only returning 0 upstream
            # can, so the two fixes work as a pair.
            #
            # LOG-EVIDENCE for the ``+ overhead_tokens`` half (log3.txt):
            #   :803  chat.compaction.overhead_estimate
            #         wire_estimate_before=156898  overhead_tokens=54673
            #   :822  provider POST .../chat/completions 200 OK
            #         derived_prompt=209814  out_prompt_tokens=209814
            #   context_limit=200000 → real send 209814, OVERSHOOT.
            #   Bare ``used`` = 156898 < 160000 → no trigger (bug).
            #   ``bytes_estimate + overhead_tokens`` = 156898 + 54673
            #   = 211571 > 160000 → correctly triggers.
            #
            # NO THRASH: overhead is per-conversation stable across a
            # turn (same runtime blocks re-assembled each time). After
            # compaction the assembly shrinks (e.g. 156898 → ~100K) so
            # ``bytes_estimate + overhead`` (~100K + 54K = 154K < 160K
            # threshold) drops back under the gate on the very next
            # turn — a single compaction settles it, no re-trigger.
            #
            # FALLBACK SAFETY: when the caller could not reverse-derive
            # overhead (``fallback_no_usage`` / ``fallback_out_of_range``
            # / era-mismatch → ``overhead_pre = 0``) both branches
            # collapse to their respective raw values and the gate
            # degrades to today's behaviour — a late trigger, never a
            # false alarm.
            used_with_overhead = max(
                eff_send,
                bytes_estimate + int(overhead_tokens or 0),
            )
            # Journal-only convenience: keep the pre-audit ``used`` field
            # visible in logs (= the assembly-side maximum before folding
            # overhead). Callers cross-referencing older log entries can
            # still find it. ``used_with_overhead`` is the value that
            # actually gates the trigger.
            used = max(eff_send, bytes_estimate)
            measured_wire_tokens = used if used > 0 else None
            threshold = int(context_limit * self._threshold_ratio)
            if used_with_overhead < threshold:
                return None
            _log.info(
                "chat.compaction.trigger",
                model_id=model_id,
                used_tokens=used,
                bytes_estimate=bytes_estimate,
                eff_send_tokens=eff_send,
                measured_eff_prompt=measured_eff_prompt,
                overhead_tokens_at_trigger=int(overhead_tokens or 0),
                used_with_overhead=used_with_overhead,
                context_limit=context_limit,
                threshold=threshold,
                messages_before=len(assembled),
                anchor=anchor_index,
            )

        # Reusable "outcome sink" the compressor writes structured algorithm
        # results into (currently ``phase_reached`` — the last of the 4 drop
        # phases that ran, 0..4). Populated only when the port impl supports
        # the tail-appended ``outcome_sink`` kwarg (AGENTS.md §3.1); stubs /
        # older fakes ignore it and the field stays absent, which the journal
        # records as ``None`` — no crash.
        outcome_sink: dict[str, Any] = {}
        try:
            # Window-anchored compression: hand the model context window to the
            # compressor so it targets ``budget × ~0.35`` (and protects the most
            # recent ``PROTECT_WINDOW_RATIO`` verbatim). ``wire_actual_tokens``
            # feeds the compressor the conversation's REAL provider-measured
            # wire size so it derives a true token density (no tokenizer call).
            budget_tokens = context_limit
            ratios = await self.resolve_ratios()
            effective_target_ratio = ratios["target"]
            effective_protect_ratio = ratios["protect"]
            if target_window_ratio_override is not None:
                # Context-overflow recovery: aim strictly lower than the target
                # that just failed. ``protect`` must come down with it — the
                # compressor refuses to let the protected region exceed the
                # target (it would make a shrinking pass impossible), so an
                # un-clamped protect would silently drag the target back UP to
                # the protect value and reproduce the wire we just had rejected.
                effective_target_ratio = float(target_window_ratio_override)
                effective_protect_ratio = min(
                    effective_protect_ratio, effective_target_ratio
                )
                _log.info(
                    "chat.compaction.target_ratio_overridden",
                    checkpoint_key=self._full_key(checkpoint_key),
                    resolved_target_ratio=ratios["target"],
                    resolved_protect_ratio=ratios["protect"],
                    effective_target_ratio=effective_target_ratio,
                    effective_protect_ratio=effective_protect_ratio,
                )
            compressed = await self._compressor.compress(
                assembled,
                target_ratio=self._target_ratio,
                preserve_tail=self._preserve_tail,
                budget_tokens=budget_tokens,
                protect_ratio=effective_protect_ratio,
                wire_actual_tokens=measured_wire_tokens,
                target_window_ratio=effective_target_ratio,
                outcome_sink=outcome_sink,
                prune_redundant=self._prune_redundant,
            )
        except Exception as exc:  # noqa: BLE001 — compression is best-effort
            _log.warning(
                "chat.compaction.failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

        # P0-4 (Task L): force-path no-op guard. When ``force=True`` we skip
        # the ``used < threshold`` gate above, so a steady-state ``/compact``
        # over an already-converged wire lands here with a compressed list
        # that is essentially the input — no real savings. Without this
        # guard we would still stamp a fresh checkpoint (new ``anchor_index``,
        # rewritten ``estimated_tokens``, ledger overwritten with an
        # equivalent copy) and journal a bytes_before ≈ bytes_after event —
        # pure IO noise that also erases the prior ckpt's bootstrap metadata.
        # Compare on the SAME口径 the trigger uses (``estimate_wire_tokens``)
        # so "materially compacted" is measured against the wire the model
        # actually sees, not a private compressor metric.
        #
        # Two independent comparisons, both required:
        #
        # (a) vs the INPUT wire — did this pass shrink anything at all?
        # (b) vs the PRIOR checkpoint — is the result actually NEW?
        #
        # (b) was missing and it made repeated ``/compact`` calls wasteful in
        # the most common real usage: because ``assembled_wire`` on the force
        # path is the RAW history (not the already-compacted wire), (a) always
        # passes, so back-to-back ``/compact`` invocations kept re-deriving a
        # byte-identical compacted head, re-stamping the checkpoint and — the
        # expensive part — re-kicking a 20-40s digest LLM call that could only
        # produce the summary we already had. Observed live: two ``/compact``
        # runs 16s apart both logged ``bytes_after=256975 messages_after=134``
        # and both burned a full digest round-trip.
        #
        # Second comparison is IDENTITY, not size. The obvious "did we gain
        # enough vs the prior checkpoint" formulation is wrong in the exact
        # case that matters: after real new history arrives, the fresh
        # compacted wire is legitimately LARGER than the old one (it covers
        # more turns), so a size-gain test rejects the update and freezes the
        # checkpoint forever. What actually identifies "nothing happened" is
        # that the compressor produced the SAME wire we already store.
        if force:
            before_wire_tokens = _estimate_wire_tokens(
                assembled, model_hint=model_id,
            )
            after_wire_tokens = _estimate_wire_tokens(
                compressed, model_hint=model_id,
            )
            if after_wire_tokens >= before_wire_tokens:
                _log.info(
                    "chat.compaction.force_no_op",
                    reason="no_gain_vs_input",
                    checkpoint_key=self._full_key(checkpoint_key),
                    model_id=model_id,
                    before_wire_tokens=before_wire_tokens,
                    after_wire_tokens=after_wire_tokens,
                    messages_before=len(assembled),
                    messages_after=len(compressed),
                )
                return None
            prior = self.get(checkpoint_key)
            if prior is not None and _same_wire(prior.compacted_wire, compressed):
                _log.info(
                    "chat.compaction.force_no_op",
                    reason="identical_to_prior_checkpoint",
                    checkpoint_key=self._full_key(checkpoint_key),
                    model_id=model_id,
                    after_wire_tokens=after_wire_tokens,
                    messages=len(compressed),
                )
                return None

        # Bootstrap estimate of the compacted wire's prompt tokens (bytes/4,
        # cloud-first token accounting). Task L P0-5: uses
        # :meth:`ThreeLevelContextCompressor._estimate_bytes` so this口径
        # matches the P0-1 compressor helper (folds ``tool_calls`` args,
        # counts UTF-8 bytes not Python chars — CJK / emoji no longer
        # under-counted by 2-3×). Bootstrap value the badge returns until
        # the next real turn's provider-measured usage lands.
        # ``estimated_tokens`` MUST use the SAME estimator the ``/compact``
        # reply reports (``estimate_wire_tokens`` — bytes/4 upgraded to a real
        # tiktoken pass past ``BPE_THRESHOLD_BYTES``, Task J). The engine
        # previously used the raw ``_estimate_bytes // 4`` shortcut, so the
        # composer badge (which reads this field) and the ``/compact`` reply
        # disagreed by ~17% on the very same compacted wire — two different
        # numbers for one artefact on one screen.
        #
        # P3-1 (Task O): share the defensive-copy list between the estimate
        # call and the ``CompactionCheckpoint(compacted_wire=...)`` build
        # below — previously each site ran its own comprehension, allocating a
        # redundant list of shallow-copied dicts. The copy also guards against
        # future in-place mutation of the compressor's working list before the
        # checkpoint is stamped.
        compacted_wire_copy = [dict(m) for m in compressed]
        est = _estimate_wire_tokens(compacted_wire_copy, model_hint=model_id) + int(overhead_tokens or 0)
        # P1.c: fold this turn's newly-observed tool-call references into the
        # ledger CARRIED FROM THE PRIOR CHECKPOINT (or start a fresh one when
        # no prior existed). Successive compactions accumulate — a file the
        # model touched two turns ago stays surfaced on the wire until the
        # ledger's own LRU / cap policy evicts it. When both the prior ledger
        # and the incoming ``new_refs`` are empty / absent we store ``None``
        # so the checkpoint's serialised shape stays byte-identical to a pre-
        # P1.c row (no spurious empty ledger persisted).
        merged_ledger: ReferenceLedger | None
        if prev_ckpt is not None and prev_ckpt.reference_ledger is not None:
            # Start from a copy of the prior ledger so ``add`` mutations
            # do not leak into the still-cached prev checkpoint (which the
            # write-through below is about to REPLACE — but a follow-up
            # reader that captured the object before the replacement must
            # NOT observe our mutations).
            merged_ledger = ReferenceLedger()
            merged_ledger.merge(prev_ckpt.reference_ledger)
        else:
            merged_ledger = ReferenceLedger() if new_refs else None
        if new_refs and merged_ledger is not None:
            for ref in new_refs:
                merged_ledger.add(ref)
        # Empty ledger collapses back to ``None`` so an empty ``new_refs``
        # over a ``None`` prior stays visually indistinguishable from the
        # pre-P1.c layout on disk / on the wire.
        if merged_ledger is not None and merged_ledger.is_empty():
            merged_ledger = None
        # Task R: monotonically advance the generation on the frozen
        # checkpoint. Legacy rows (loaded from a pre-migration-065 DB)
        # already default to generation 1 on the dataclass side; a fresh
        # compaction bumps the counter so the store's CAS-on-generation
        # (``save_core``) can reject any peer whose snapshot lags behind.
        new_generation = (
            int(prev_ckpt.generation) + 1 if prev_ckpt is not None else 1
        )
        ckpt = CompactionCheckpoint(
            anchor_index=anchor_index,
            compacted_wire=compacted_wire_copy,
            estimated_tokens=est or None,
            anchor_message_id=anchor_message_id,
            reference_ledger=merged_ledger,
            generation=new_generation,
        )
        full_key = self._full_key(checkpoint_key)
        # Persist FIRST with CAS on generation. On a successful advance we
        # own the row and the in-memory cache is swapped to match; on a
        # CAS refusal a peer already wrote a strictly-newer state — we
        # abandon this checkpoint and DO NOT touch the cache (the peer's
        # write-through has already installed the winning frozen ckpt).
        # Without a store we fall back to the pure-memory path (best-
        # effort behaviour byte-identical to pre-Task-R when no store is
        # wired — the in-memory cache is the source of truth).
        wrote_through = True
        if self._checkpoint_store is not None and persist_id is not None:
            try:
                wrote_through = await self._checkpoint_store.save_core(
                    persist_id, ckpt,
                )
            except Exception as exc:  # noqa: BLE001 — never break the turn
                _log.warning(
                    "chat.compaction.checkpoint_persist_failed",
                    conversation_id=persist_id.value,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                wrote_through = False
        if not wrote_through:
            _log.info(
                "chat.compaction.maybe_compress_cas_refused",
                checkpoint_key=full_key,
                incoming_generation=new_generation,
                reason="peer_advanced_or_persist_failed",
            )
            return None
        self._checkpoints[full_key] = ckpt
        # Mark this conversation as "checkpoint-loaded" — the in-memory
        # copy we just installed is authoritative and no later lazy-load
        # should overwrite it. Fragment tables land via replace_*_fragment
        # callbacks on the async P2/P3 kick paths that follow.
        self._loaded.add(full_key)
        # Compute the before/after byte-footprint once — the ``checkpoint_stored``
        # structured log AND the journal event both need them, so hoisting
        # avoids counting each list twice.
        bytes_before = sum(
            len(str(m.get("content") or "").encode("utf-8")) for m in assembled
        )
        bytes_after = sum(
            len(str(m.get("content") or "").encode("utf-8")) for m in compressed
        )
        messages_before = len(assembled)
        messages_after = len(compressed)
        _log.info(
            "chat.compaction.checkpoint_stored",
            anchor=anchor_index,
            messages_after=messages_after,
            messages_before=messages_before,
            bytes_before=bytes_before,
            bytes_after=bytes_after,
            est_compacted_tokens=est,
        )
        # ------------------------------------------------------------------
        # Best-effort JSONL journal (CONTEXT-COMPRESSION-NEXT §5.1). Fire-and-
        # forget: the journal's own ``append`` swallows exceptions to debug
        # logs, and we still dispatch via :func:`asyncio.create_task` so a
        # future synchronous journal implementation cannot block the caller.
        # ``digest_refreshed`` / ``reference_ledger_count`` are P1/P2 fields
        # not owned by this phase — recorded as ``False``/``0`` so the JSONL
        # schema is stable from day one and later phases just flip values.
        # ------------------------------------------------------------------
        if self._journal is not None:
            journal_event: dict[str, Any] = {
                "conversation_id": full_key,
                "model_id": model_id,
                "trigger_reason": trigger_reason,
                "used_tokens": used,
                "eff_send_tokens": eff_send,
                "bytes_estimate": bytes_estimate,
                "measured_eff_prompt": measured_eff_prompt,
                "context_limit": context_limit,
                "threshold": threshold,
                "messages_before": messages_before,
                "messages_after": messages_after,
                "bytes_before": bytes_before,
                "bytes_after": bytes_after,
                "anchor_before": prev_anchor,
                "anchor_after": ckpt.anchor_index,
                "est_compacted_tokens": ckpt.estimated_tokens,
                "phase_reached": outcome_sink.get("phase_reached"),
                # Phase 0 (redundant tool-group prune) observation block:
                # ``{total, superseded, useless, saved_bytes, saved_tokens}``,
                # or ``None`` when Phase 0 was off / pruned nothing. Appended
                # to the JSONL schema (append-only) so the free prune's real
                # production yield is auditable alongside the lossy phases.
                "pruned_groups": outcome_sink.get("pruned_groups"),
                "live_wire_mode": live_wire_mode,
                "force": force,
                # P1/P2 fields (digest refresh + reference-ledger count).
                # Stable JSONL schema from day one — the concrete values are
                # filled in later phases; this phase pins the field names.
                "digest_refreshed": False,
                "reference_ledger_count": (
                    len(merged_ledger.files)
                    + len(merged_ledger.urls)
                    + len(merged_ledger.execs)
                    if merged_ledger is not None
                    else 0
                ),
            }
            try:
                asyncio.create_task(self._journal.append(journal_event))
            except RuntimeError as exc:
                # ``create_task`` raises ``RuntimeError`` iff there is no
                # running event loop (e.g. a sync test harness driving the
                # engine outside an ``asyncio.run`` frame). Every other
                # failure would have to come from ``journal.append`` itself,
                # which is dispatched — not awaited — so it cannot raise
                # into this frame. P3-3 (Task O): narrowed from ``except
                # Exception`` to make the true failure surface explicit and
                # stop hiding actual bugs behind a "best-effort" veneer.
                _log.debug(
                    "chat.compaction.journal_dispatch_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        return ckpt
