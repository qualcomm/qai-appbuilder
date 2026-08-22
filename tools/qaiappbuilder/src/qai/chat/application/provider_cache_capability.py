# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Runtime detection of prompt-cache support, gating tool-output aging.

Cache-hit behaviour is a MODEL-level property, keyed on ``model_hint``. Two
models sharing the SAME gateway (same base_url / key) can behave differently:
one may replay history as ``cache_read`` (real hits) while another only ever
echoes ``cache_write`` and never reads back. Keying on base_url would let the
first model to report clobber the other's state, so each model_hint is learned
independently.

The learning signal is the round's ACTUAL cache-read volume (tokens truly read
back from the cache), NOT merely "the gateway echoed some cache field". A model
that only writes (``cache_write`` every round, ``cache_read`` always 0) does
NOT support caching from our perspective — surfacing a write field does not
prove a read ever lands. Judging by "any cache field present" mistakes such a
write-only model for a cache-supporting one, disables aging, and thereby
prevents the byte-stable frozen prefix that anchors the cache breakpoint — a
self-defeating loop where the model can never hit cache.

State machine (per model_hint, three states). The observation unit is ONE
TURN, not one round — see ``_NO_CACHE_ROUNDS`` below:

  * ``UNKNOWN``  — no turn observed yet, or fewer than ``_NO_CACHE_ROUNDS``
                   consecutive zero-read turns and no hit yet. Aging OFF:
                   give the cache a chance to establish + hit before deciding.
  * ``SUPPORTS`` — a turn observed ``cache_read > 0`` (a real hit). Aging OFF
                   so the cached prefix stays byte-clean. Does NOT revert on an
                   occasional zero-read turn (a hit proves support).
  * ``NO_CACHE`` — ``_NO_CACHE_ROUNDS`` consecutive turns each had a request
                   but ``cache_read == 0``, and no hit was ever seen. Aging ON.

``NO_CACHE`` is a STICKY ABSORBING STATE: once entered, aging stays ON for the
rest of the process even if ``cache_read`` later turns positive, and it is
NEVER revisited by ``mark``. This is DELIBERATE anti-thrash design, not a bug —
do NOT "fix" it into a bidirectional transition:

  A write-only model only starts hitting cache BECAUSE aging ON produced the
  frozen-prefix anchor. If a subsequent hit flipped it back to SUPPORTS and
  turned aging OFF, the anchor would vanish → the hit would be lost → aging ON
  again → oscillation. Pinning aging ON breaks that cycle. The pin is
  process-scoped only: this registry is in-memory and never persisted, so a
  process restart re-learns from UNKNOWN (one round of aging-off) — that is the
  accepted, cheap recovery path for a gateway that later gains real read-back.

The first zero-read TURN for a fresh model is EXPECTED (a pure cache-write
establishing turn reads nothing), so ``_NO_CACHE_ROUNDS == 2``: only a SECOND
consecutive zero-read TURN condemns the model to NO_CACHE.

CALLERS MUST AGGREGATE PER TURN. ``mark`` counts every call it receives, so a
caller that marks each agentic ROUND lets ONE multi-round turn spend both
credits — which condemned a model on its very first turn in the field
(log3.txt 21:45:01) even though it later read cache back at 99.98%. The main
agent therefore records each round's signal on its turn state and flushes the
turn's MAXIMUM exactly once (``StreamChatUseCase._flush_cache_capability``).
A landed read on ANY round proves support, so the max loses no information.

Process-lifetime, in-memory, never persisted. Shared by BOTH the main agent
(StreamChatUseCase) and the sub-agent (AgentToolHandler) via a single
DI-injected instance.
"""
from __future__ import annotations

from qai.platform.logging import get_logger

_log = get_logger(__name__)

# UNKNOWN → NO_CACHE requires this many CONSECUTIVE zero-read rounds. The first
# round of a fresh model is a pure cache-write establishing round (reads
# nothing), so 1 would misjudge every model; 2 waits for a second confirming
# zero-read round before turning aging ON.
_NO_CACHE_ROUNDS = 2

_UNKNOWN = "unknown"
_SUPPORTS = "supports"
_NO_CACHE = "no_cache"


class ProviderCacheCapabilityRegistry:
    __slots__ = ("_state", "_zero_streak")

    def __init__(self) -> None:
        # model_hint -> state (_UNKNOWN / _SUPPORTS / _NO_CACHE). Missing = UNKNOWN.
        self._state: dict[str, str] = {}
        # model_hint -> consecutive zero-cache_read round count (only meaningful
        # while UNKNOWN; frozen once SUPPORTS/NO_CACHE is reached).
        self._zero_streak: dict[str, int] = {}

    def mark(self, model_hint: str | None, cache_read_tokens: int) -> None:
        """Record one round's ACTUAL cache-read volume for ``model_hint``.

        ``cache_read_tokens`` is the real number of tokens read back from the
        cache this round (0 when nothing was read). Positive → a real hit.
        """
        if not model_hint:
            return
        state = self._state.get(model_hint, _UNKNOWN)
        # NO_CACHE is absorbing (sticky anti-thrash): never leaves, aging pinned ON.
        if state == _NO_CACHE:
            # DIAG (sticky-absorbing proof): the gateway started reading cache
            # back AFTER we condemned this model, yet the state stays pinned.
            # That is the DELIBERATE anti-thrash design (see module docstring),
            # not a bug — this log is the evidence it behaves as designed
            # rather than silently ignoring a real hit.
            if cache_read_tokens > 0:
                _log.info(
                    "chat.diag.cache_capability_sticky_hit",
                    model_hint=model_hint,
                    state=state,
                    cache_read_tokens=cache_read_tokens,
                    sticky_by_design=True,
                    zero_read_streak=self._zero_streak.get(model_hint, 0),
                )
            self._log_mark(
                model_hint=model_hint,
                state_before=state,
                state_after=state,
                cache_read_tokens=cache_read_tokens,
            )
            return
        if cache_read_tokens > 0:
            # A real hit proves support. SUPPORTS never reverts on a later zero.
            self._state[model_hint] = _SUPPORTS
            self._zero_streak[model_hint] = 0
            if state != _SUPPORTS:
                self._log_transition(
                    model_hint=model_hint,
                    state_before=state,
                    state_after=_SUPPORTS,
                    transition_reason="cache_read_observed",
                    cache_read_tokens=cache_read_tokens,
                )
            self._log_mark(
                model_hint=model_hint,
                state_before=state,
                state_after=_SUPPORTS,
                cache_read_tokens=cache_read_tokens,
            )
            return
        # Zero read this round.
        if state == _SUPPORTS:
            self._log_mark(
                model_hint=model_hint,
                state_before=state,
                state_after=state,
                cache_read_tokens=cache_read_tokens,
            )
            return  # a proven-supporting model tolerates an occasional zero read
        streak = self._zero_streak.get(model_hint, 0) + 1
        self._zero_streak[model_hint] = streak
        if streak >= _NO_CACHE_ROUNDS:
            self._state[model_hint] = _NO_CACHE
            self._log_transition(
                model_hint=model_hint,
                state_before=state,
                state_after=_NO_CACHE,
                transition_reason="zero_read_streak_exhausted",
                cache_read_tokens=cache_read_tokens,
            )
        self._log_mark(
            model_hint=model_hint,
            state_before=state,
            state_after=self._state.get(model_hint, _UNKNOWN),
            cache_read_tokens=cache_read_tokens,
        )

    # ------------------------------------------------------------------
    # Diagnostics (observation-only; never influence a decision)
    # ------------------------------------------------------------------
    def _log_mark(
        self,
        *,
        model_hint: str,
        state_before: str,
        state_after: str,
        cache_read_tokens: int,
    ) -> None:
        """Emit the per-round capability observation.

        ``known_model_count`` is the number of model_hints this registry has
        learned so far — the multi-model isolation witness: a freshly routed
        model shows ``state_before=unknown`` while the count grows, proving
        each model_hint is learned independently rather than clobbering a peer.
        """
        _log.info(
            "chat.diag.cache_capability_mark",
            model_hint=model_hint,
            state_before=state_before,
            state_after=state_after,
            cache_read_tokens=cache_read_tokens,
            zero_read_streak=self._zero_streak.get(model_hint, 0),
            no_cache_rounds_threshold=_NO_CACHE_ROUNDS,
            known_model_count=len(self._state),
        )

    def _log_transition(
        self,
        *,
        model_hint: str,
        state_before: str,
        state_after: str,
        transition_reason: str,
        cache_read_tokens: int,
    ) -> None:
        """Emit a state-machine EDGE (UNKNOWN→SUPPORTS / UNKNOWN→NO_CACHE)."""
        _log.info(
            "chat.diag.cache_capability_transition",
            model_hint=model_hint,
            state_before=state_before,
            state_after=state_after,
            transition_reason=transition_reason,
            cache_read_tokens=cache_read_tokens,
            zero_read_streak=self._zero_streak.get(model_hint, 0),
            no_cache_rounds_threshold=_NO_CACHE_ROUNDS,
        )

    def aging_enabled(self, model_hint: str | None) -> bool:
        """True → run aging (model has NO cache read-back). False → skip aging
        (supports cache, OR still UNKNOWN = default assume-supports so the cache
        gets a chance to establish + hit before we decide)."""
        if not model_hint:
            # unknown → assume supports → aging OFF
            _log.info(
                "chat.diag.cache_capability_gate",
                model_hint=None,
                state=_UNKNOWN,
                aging_enabled=False,
                known_model_count=len(self._state),
            )
            return False
        state = self._state.get(model_hint, _UNKNOWN)
        enabled = state == _NO_CACHE
        _log.info(
            "chat.diag.cache_capability_gate",
            model_hint=model_hint,
            state=state,
            aging_enabled=enabled,
            zero_read_streak=self._zero_streak.get(model_hint, 0),
            known_model_count=len(self._state),
        )
        return enabled


__all__ = ["ProviderCacheCapabilityRegistry"]
