# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Convergence-control feature flags — central constants + resolver (DISC-2 P1).

The "discussion convergence control" feature (DISC-2 二期) introduces four
user-facing switches stored under ``Conversation.meta["discussion"]`` (§3.1
tail-append, no migration).  This module is the SINGLE source of truth for:

* the wire/meta **key names** of the four flags;
* the **new-conversation defaults** (用户 2026-06-23 拍板:新建默认开 + 保守软
  停止;§22A.8 / §22A.11#2) — used ONLY when seeding a brand-new conversation
  and for the front-end default UI;
* the legal ``soft_stop_mode`` value set;
* :func:`resolve_convergence_flags` — the orchestrator's unified read entry that
  coerces a ``discussion`` dict into a frozen :class:`ConvergenceFlags`.

**Read-side semantics (critical — keeps the 31 deep_task cases byte-for-byte):**
a *missing* key in ``meta["discussion"]`` resolves to **OFF** (legacy / test /
existing conversations are untouched).  The new-conversation defaults below are
applied ONLY at conversation-creation / front-end-default time — they are NEVER
auto-applied to a missing key at orchestrator read time.

Layering: ``application/use_cases`` — pure constants + a pure function over
stdlib only.  No ports / domain / adapters, so the layering contracts hold.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CONVERGENCE_CONTROL_ENABLED_KEY",
    "MANAGER_EARLY_END_ENABLED_KEY",
    "SOFT_STOP_ENABLED_KEY",
    "SOFT_STOP_MODE_KEY",
    "DEFAULT_SOFT_STOP_MODE",
    "SOFT_STOP_MODES",
    "MIN_TURNS_BEFORE_END",
    "SOFT_STOP_SIMILARITY_KEY",
    "SOFT_STOP_MIN_ROUNDS_KEY",
    "SOFT_STOP_CONSECUTIVE_KEY",
    "DEFAULT_SOFT_STOP_SIMILARITY",
    "DEFAULT_SOFT_STOP_MIN_ROUNDS",
    "DEFAULT_SOFT_STOP_CONSECUTIVE",
    "SoftStopThresholds",
    "resolve_soft_stop_thresholds",
    "NEW_CONVERSATION_CONVERGENCE_DEFAULTS",
    "ConvergenceFlags",
    "resolve_convergence_flags",
]


# ── Flag key names (wire snake_case == meta["discussion"] key) ───────────────
CONVERGENCE_CONTROL_ENABLED_KEY = "convergence_control_enabled"
MANAGER_EARLY_END_ENABLED_KEY = "manager_early_end_enabled"
SOFT_STOP_ENABLED_KEY = "soft_stop_enabled"
SOFT_STOP_MODE_KEY = "soft_stop_mode"


# ── soft_stop_mode legal values ──────────────────────────────────────────────
#: Default soft-stop mode (conservative = least aggressive convergence).
DEFAULT_SOFT_STOP_MODE = "conservative"

#: Legal ``soft_stop_mode`` values.  P1-step1 only ships the conservative mode;
#: P1-step4 will extend this tuple (e.g. "balanced" / "aggressive") once the
#: SoftStop signal providers land.  Until then any non-"conservative" value
#: coerces back to the default (see :func:`resolve_convergence_flags`).
SOFT_STOP_MODES: tuple[str, ...] = ("conservative",)


# ── Shared minimum-rounds floor (§22A.4) ─────────────────────────────────────
#: Minimum number of completed rounds before any convergence early-stop may
#: fire.  Shared lower bound (§22A.4): the Manager early-END (P1-step3) refuses
#: to end at-or-before this round, and the SoftStop soft-stop (P1-step4) reuses
#: the SAME floor so neither path can conclude a discussion prematurely.  Kept as
#: a code constant (NOT a meta key) to minimise the change surface — there is no
#: per-conversation override for the floor.
MIN_TURNS_BEFORE_END = 3


# ── User-tunable soft-stop thresholds (§22A.4, TODO-2) ───────────────────────
# The three soft-stop tuning knobs the user asked to be configurable (相似度 /
# min 轮 / 连续轮).  Stored under ``meta["discussion"]``; a missing key resolves
# to the conservative default below (legacy conversations untouched).  These
# DEFAULTS are the canonical source for both the read-side resolver AND the
# module-level constants in :mod:`discussion_convergence_rules` (which import
# them), so there is a single source of truth.
SOFT_STOP_SIMILARITY_KEY = "soft_stop_similarity"
SOFT_STOP_MIN_ROUNDS_KEY = "soft_stop_min_rounds"
SOFT_STOP_CONSECUTIVE_KEY = "soft_stop_consecutive_turns"

#: Char-trigram Jaccard at-or-above which a turn is "highly similar"
#: (near-restatement).  0.72 is conservative — true paraphrases clear it, new
#: content stays below.  Clamp range [0.50, 0.99] at read time.
DEFAULT_SOFT_STOP_SIMILARITY = 0.72
#: Minimum completed rounds before any soft-stop may fire (shared floor, §22A.4).
#: 3 = the conservative default.  Clamp range [1, 50] at read time.
DEFAULT_SOFT_STOP_MIN_ROUNDS = MIN_TURNS_BEFORE_END
#: How many CONSECUTIVE high-score turns are required before soft-stopping
#: (anti-误砍 guard).  2 = a single noisy turn can never end a discussion.
#: Clamp range [1, 10] at read time.
DEFAULT_SOFT_STOP_CONSECUTIVE = 2


#: New-conversation defaults (用户 2026-06-23 拍板:新建默认开 + 保守软停止;
#: §22A.8 / §22A.11#2).  Applied ONLY when seeding a brand-new conversation and
#: mirrored by the front-end default UI — NOT at orchestrator read time (a
#: missing key always resolves to OFF, keeping legacy/existing conversations and
#: the 31 deep_task cases byte-for-byte unchanged).
NEW_CONVERSATION_CONVERGENCE_DEFAULTS: dict[str, object] = {
    CONVERGENCE_CONTROL_ENABLED_KEY: True,
    MANAGER_EARLY_END_ENABLED_KEY: True,
    SOFT_STOP_ENABLED_KEY: True,
    SOFT_STOP_MODE_KEY: DEFAULT_SOFT_STOP_MODE,
}


@dataclass(frozen=True, slots=True)
class ConvergenceFlags:
    """The resolved convergence-control flags for ONE conversation.

    Produced by :func:`resolve_convergence_flags`.  Every bool defaults to
    ``False`` (= OFF for a missing key) and ``soft_stop_mode`` defaults to
    ``"conservative"`` so a flag-less discussion behaves exactly like the
    DISC-2 一期 implementation.
    """

    convergence_control_enabled: bool = False
    manager_early_end_enabled: bool = False
    soft_stop_enabled: bool = False
    soft_stop_mode: str = DEFAULT_SOFT_STOP_MODE


def _coerce_bool(value: object) -> bool:
    """Coerce a meta value to bool (missing/None/falsey/illegal → ``False``).

    Mirrors the orchestrator's ``_coerce_bool`` style (``bool(value)``); a
    missing key arrives here as ``None`` → ``False`` (= OFF).
    """
    return bool(value)


def _coerce_soft_stop_mode(value: object) -> str:
    """Coerce a meta value to a legal ``soft_stop_mode`` (illegal/missing →
    :data:`DEFAULT_SOFT_STOP_MODE`).
    """
    if isinstance(value, str) and value in SOFT_STOP_MODES:
        return value
    return DEFAULT_SOFT_STOP_MODE


def resolve_convergence_flags(discussion: dict | None) -> ConvergenceFlags:
    """Resolve the four convergence flags from a ``meta["discussion"]`` dict.

    The orchestrator's unified read entry.  A ``None`` / empty dict, or any
    missing key, resolves to OFF (``soft_stop_mode`` → ``"conservative"``),
    keeping legacy/existing conversations byte-for-byte unchanged.  An illegal
    ``soft_stop_mode`` coerces back to the default.

    ``convergence_control_enabled`` is the **master switch**: when it is OFF the
    two sub-signals (``manager_early_end_enabled`` / ``soft_stop_enabled``) are
    forced OFF here regardless of their own stored value, so turning the master
    switch off truly disables the whole convergence-control feature (the
    consumers — :class:`ManagerAgentSelector` / :class:`ConvergenceController` —
    only read the resolved sub-flags).  Zero-regression: new conversations seed
    all three ON (so the resolved sub-flags are unchanged) and legacy/missing-key
    conversations have the master missing → OFF → sub-flags already OFF.
    """
    d = discussion or {}
    master = _coerce_bool(d.get(CONVERGENCE_CONTROL_ENABLED_KEY))
    return ConvergenceFlags(
        convergence_control_enabled=master,
        manager_early_end_enabled=(
            master and _coerce_bool(d.get(MANAGER_EARLY_END_ENABLED_KEY))
        ),
        soft_stop_enabled=(
            master and _coerce_bool(d.get(SOFT_STOP_ENABLED_KEY))
        ),
        soft_stop_mode=_coerce_soft_stop_mode(d.get(SOFT_STOP_MODE_KEY)),
    )


@dataclass(frozen=True, slots=True)
class SoftStopThresholds:
    """Resolved soft-stop tuning thresholds for ONE conversation (§22A.4, TODO-2).

    Produced by :func:`resolve_soft_stop_thresholds`.  Field defaults equal the
    conservative module constants so a flag-less / missing-key discussion behaves
    exactly like the pre-TODO-2 detector.
    """

    similarity: float = DEFAULT_SOFT_STOP_SIMILARITY
    min_rounds: int = DEFAULT_SOFT_STOP_MIN_ROUNDS
    consecutive_turns: int = DEFAULT_SOFT_STOP_CONSECUTIVE


def _coerce_float(value: object, *, default: float, lo: float, hi: float) -> float:
    """Coerce a meta value into a float clamped to ``[lo, hi]`` (bool rejected)."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and value == value:  # finite, not NaN
        return max(lo, min(hi, float(value)))
    return default


def _coerce_int(value: object, *, default: int, lo: int, hi: int) -> int:
    """Coerce a meta value into an int clamped to ``[lo, hi]`` (bool rejected)."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(lo, min(hi, value))
    if isinstance(value, float) and value == value:
        return max(lo, min(hi, int(value)))
    return default


def resolve_soft_stop_thresholds(discussion: dict | None) -> SoftStopThresholds:
    """Resolve the three soft-stop tuning thresholds from a ``meta`` dict (TODO-2).

    A ``None`` / empty dict or any missing key resolves to the conservative
    default; each value is coerced + clamped to a safe range so a malformed /
    hostile meta entry can neither disable nor pathologically tighten the
    detector.  Pure function over a stdlib dict.
    """
    d = discussion or {}
    return SoftStopThresholds(
        similarity=_coerce_float(
            d.get(SOFT_STOP_SIMILARITY_KEY),
            default=DEFAULT_SOFT_STOP_SIMILARITY,
            lo=0.50,
            hi=0.99,
        ),
        min_rounds=_coerce_int(
            d.get(SOFT_STOP_MIN_ROUNDS_KEY),
            default=DEFAULT_SOFT_STOP_MIN_ROUNDS,
            lo=1,
            hi=50,
        ),
        consecutive_turns=_coerce_int(
            d.get(SOFT_STOP_CONSECUTIVE_KEY),
            default=DEFAULT_SOFT_STOP_CONSECUTIVE,
            lo=1,
            hi=10,
        ),
    )
