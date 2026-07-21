# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ConvergenceController`` — single-point discussion early-stop decision (DISC-2 P1-step2).

The multi-agent discussion loop (``orchestrate_discussion._run_full_path``)
historically scatters its "should the discussion end early?" judgement across
several places: the implicit ``range(1, effective_rounds + 1)`` upper bound
(max_rounds) and the cooperative-abort ``handle.is_set()`` raises (user stop).

P1-step2 introduces this **pure application-layer component** to *converge* those
deterministic early-stop judgements into ONE observable decision point, WITHOUT
changing any user-perceivable behaviour:

* :func:`max_rounds_signal` — fires when the current round reached the budget;
* :func:`user_stop_signal` — fires when the cooperative-abort handle is set;
* :class:`ConvergenceController` — combines the signals (user-stop wins) into a
  :class:`ConvergenceDecision` the loop logs + acts on.

**Behaviour-equivalence contract (critical — keeps deep_task byte-for-byte):**
both deterministic signals are *unconditional existing behaviour* — ``max_rounds``
is the loop's hard upper bound and ``user_stop`` already ``raise``s the moment the
handle is set.  The ``convergence_control_enabled`` flag does **NOT** gate either
signal; it is wired through (constructor) so P1-step3/step4 can use it.

**DISC-2 P1-step4 — SoftStop "no new info" signal (§22A.4):**
when (and ONLY when) the ``soft_stop_enabled`` flag is on, :meth:`after_turn`
additionally scores the just-completed turn for "no new information"
(:func:`...discussion_convergence_rules.score_no_new_info`) and soft-stops the
discussion once the stall has persisted for
:data:`...discussion_convergence_rules.CONSECUTIVE_NO_NEW_TURNS` CONSECUTIVE
high-score turns, past the shared minimum-rounds floor.  Two anti-误砍 guards
(min-rounds + consecutive) keep it conservative.  When the flag is OFF the score
is never even computed → behaviour is byte-for-byte identical to P1-step3.  The
per-discussion CONSECUTIVE counter is legitimate transient run-state held on the
(per-discussion) controller instance — NOT global mutable state.

Layering: ``application/use_cases`` — depends ONLY on stdlib + the same-layer
:mod:`qai.chat.application.use_cases.convergence_defaults` +
:mod:`qai.chat.application.use_cases.discussion_convergence_rules`.  No ports /
domain / adapters / fastapi imports, so the ``layered-chat`` /
``context-isolation`` contracts (17 contracts) hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qai.chat.application.use_cases.convergence_defaults import (
    ConvergenceFlags,
    SoftStopThresholds,
)
from qai.chat.application.use_cases.discussion_convergence_rules import (
    NO_NEW_INFO_SCORE_THRESHOLD,
    score_no_new_info,
)

__all__ = [
    "ConvergenceDecision",
    "ConvergenceController",
    "max_rounds_signal",
    "user_stop_signal",
]


# ── Reason / signal name constants (observable in logs) ──────────────────────
_REASON_MAX_ROUNDS = "max_rounds"
_REASON_MANUAL_STOP = "manual_stop"
_REASON_SOFT_NO_NEW_INFO = "soft_no_new_info"
_SIGNAL_MAX_ROUNDS = "max_rounds_signal"
_SIGNAL_USER_STOP = "user_stop_signal"
_SIGNAL_SOFT_STOP = "soft_stop_signal"


@dataclass(frozen=True, slots=True)
class ConvergenceDecision:
    """The result of one :meth:`ConvergenceController.after_turn` evaluation.

    ``should_stop`` — whether the discussion loop should ``break`` now.
    ``reason`` — ``""`` when continuing; otherwise a stable machine-readable code
    (``"max_rounds"`` / ``"manual_stop"`` / ``"soft_no_new_info"``;
    ``"manager_end"`` is recorded by the orchestrator on the selector path).
    ``confidence`` — ``1.0`` for the deterministic ``max_rounds`` / ``user_stop``
    signals; for ``soft_no_new_info`` it is the normalised stall score (0..1).
    ``signals`` — the names of the signals that fired (for observability/debug).
    """

    should_stop: bool = False
    reason: str = ""
    confidence: float = 0.0
    signals: tuple[str, ...] = field(default_factory=tuple)


# ── Pure signal providers (each independently unit-testable) ─────────────────
def max_rounds_signal(round_index: int, effective_rounds: int) -> bool:
    """Fire when the current round reached the effective round budget.

    Mirrors the loop's implicit ``range(1, effective_rounds + 1)`` upper bound:
    once ``round_index >= effective_rounds`` the loop would naturally exhaust on
    the next iteration, so this is the same hard cap surfaced as a signal.
    """
    return round_index >= effective_rounds


def user_stop_signal(user_stop_set: bool) -> bool:
    """Fire when the cooperative-abort handle is set (user pressed Stop).

    A thin pass-through so the controller treats user-stop as a first-class
    signal.  The orchestrator still ``raise``s ``ChatStreamAbortedError`` the
    instant the handle is set (mid-stream) — this signal is the *post-turn*
    convergence view of the same truth.
    """
    return user_stop_set


class ConvergenceController:
    """Single-point early-stop decision for the discussion loop.

    Constructed with the resolved :class:`ConvergenceFlags` for ONE conversation.
    The deterministic ``max_rounds`` / ``user_stop`` signals are NOT flag-gated
    (they are unconditional existing behaviour); the ``soft_stop`` "no new info"
    signal (P1-step4) is gated by ``flags.soft_stop_enabled`` and only fires after
    the minimum-rounds floor AND a run of CONSECUTIVE high-score turns.

    The controller is a PER-DISCUSSION instance (the orchestrator constructs one
    per ``_run_full_path``), so the ``_consecutive_no_new`` run-counter it carries
    is legitimate transient per-discussion state — NOT global mutable state.
    """

    __slots__ = ("_flags", "_consecutive_no_new", "_thresholds")

    def __init__(
        self,
        flags: ConvergenceFlags,
        thresholds: SoftStopThresholds | None = None,
    ) -> None:
        self._flags = flags
        # User-tunable soft-stop thresholds (TODO-2). ``None`` ⇒ the conservative
        # defaults (== the legacy module constants), so an un-configured /
        # missing-key discussion is byte-for-byte the pre-TODO-2 behaviour.
        self._thresholds = thresholds or SoftStopThresholds()
        # Per-discussion run-state: how many CONSECUTIVE turns have just scored
        # at-or-above the "no new info" threshold.  Reset to 0 by any turn that
        # carries new information, so a momentary lull never accumulates toward a
        # soft-stop (anti-误砍).  Only mutated when ``soft_stop_enabled`` is on.
        self._consecutive_no_new = 0

    @property
    def flags(self) -> ConvergenceFlags:
        """The convergence flags this controller was constructed with."""
        return self._flags

    def after_turn(
        self,
        *,
        round_index: int,
        effective_rounds: int,
        user_stop_set: bool,
        current_text: str = "",
        recent_texts: tuple[str, ...] = (),
        same_role_prev_text: str | None = None,
        manager_recently_suggested_end: bool = False,
    ) -> ConvergenceDecision:
        """Decide whether to stop the discussion AFTER a completed speaker turn.

        Priority (most-significant first): ``user_stop`` (``"manual_stop"``) >
        ``soft_stop`` (``"soft_no_new_info"``) > ``max_rounds`` (``"max_rounds"``).
        ``manager_end`` is handled on the selector path by the orchestrator (a
        ``None`` from ``select_next``), so it is not evaluated here.

        ``user_stop`` / ``max_rounds`` are always evaluated — the existing
        unconditional stop conditions (NOT gated by any flag), so a flag-less /
        flags-OFF discussion is byte-for-byte identical to the legacy loop.

        The text arguments feed the P1-step4 SoftStop scorer and are used ONLY
        when ``flags.soft_stop_enabled`` is on; when OFF the score is never even
        computed and the per-discussion counter never advances — keeping the
        flag-OFF path identical to P1-step3.
        """
        user_stop = user_stop_signal(user_stop_set)
        max_rounds = max_rounds_signal(round_index, effective_rounds)

        if user_stop:
            # User-stop wins regardless of round count (most-significant signal).
            signals: tuple[str, ...] = (_SIGNAL_USER_STOP,)
            if max_rounds:
                signals = (_SIGNAL_USER_STOP, _SIGNAL_MAX_ROUNDS)
            return ConvergenceDecision(
                should_stop=True,
                reason=_REASON_MANUAL_STOP,
                confidence=1.0,
                signals=signals,
            )

        # SoftStop (§22A.4) — gated by the flag; advances/resets the per-discussion
        # consecutive-stall counter and fires only past the min-rounds floor after
        # CONSECUTIVE_NO_NEW_TURNS consecutive high-score turns (double anti-误砍).
        soft = self._evaluate_soft_stop(
            round_index=round_index,
            current_text=current_text,
            recent_texts=recent_texts,
            same_role_prev_text=same_role_prev_text,
            manager_recently_suggested_end=manager_recently_suggested_end,
        )
        if soft is not None:
            return soft

        if max_rounds:
            return ConvergenceDecision(
                should_stop=True,
                reason=_REASON_MAX_ROUNDS,
                confidence=1.0,
                signals=(_SIGNAL_MAX_ROUNDS,),
            )
        return ConvergenceDecision()

    def _evaluate_soft_stop(
        self,
        *,
        round_index: int,
        current_text: str,
        recent_texts: tuple[str, ...],
        same_role_prev_text: str | None,
        manager_recently_suggested_end: bool,
    ) -> ConvergenceDecision | None:
        """Update the consecutive-stall counter and return a SoftStop decision.

        Returns a ``soft_no_new_info`` :class:`ConvergenceDecision` when the
        stall has persisted long enough past the floor; ``None`` to keep going.
        A no-op (counter untouched, returns ``None``) when ``soft_stop_enabled``
        is OFF — so the flag-OFF path is byte-for-byte the P1-step3 behaviour.
        """
        if not self._flags.soft_stop_enabled:
            return None

        score, fired = score_no_new_info(
            current_text=current_text,
            recent_texts=list(recent_texts),
            same_role_prev_text=same_role_prev_text,
            manager_recently_suggested_end=manager_recently_suggested_end,
            high_similarity_threshold=self._thresholds.similarity,
        )
        if score >= NO_NEW_INFO_SCORE_THRESHOLD:
            self._consecutive_no_new += 1
        else:
            self._consecutive_no_new = 0

        # Anti-误砍 guard #1 (min-rounds floor) + #2 (consecutive run): never fire
        # before the configured floor, and only after a sustained stall.
        if (
            round_index >= self._thresholds.min_rounds
            and self._consecutive_no_new >= self._thresholds.consecutive_turns
        ):
            # Normalise the (0..5) stall score onto 0..1 for ``confidence``.
            confidence = min(score / 5.0, 1.0)
            return ConvergenceDecision(
                should_stop=True,
                reason=_REASON_SOFT_NO_NEW_INFO,
                confidence=confidence,
                signals=(_SIGNAL_SOFT_STOP, *fired),
            )
        return None
