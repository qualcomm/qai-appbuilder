# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Deterministic SoftStop scoring rules — "no new information" detection (DISC-2 P1-step4).

The Manager early-END signal (P1-step3) lets an LLM moderator conclude a
discussion, but the round-robin selector has no Manager — and even a Manager can
let a discussion spin on repeating itself.  §22A.4 adds a SECOND, fully
deterministic convergence兜底: a **scored "no new information" detector** that,
when the ``soft_stop_enabled`` flag is on, stops a discussion that has stalled
into repetition / empty filler — but ONLY after a minimum number of rounds AND
only after CONSECUTIVE high-score turns (double anti-误砍 guard, conservative).

This module owns the PURE, side-effect-free scoring primitives the
:class:`~qai.chat.application.use_cases.convergence_controller.ConvergenceController`
composes:

* :data:`SIMILARITY_WINDOW_TURNS` … :data:`SHORT_RESPONSE_CHARS` — the
  conservative threshold constants (§22A.4; tuned to the LEAST-aggressive end so
  the detector never砍 a still-productive discussion).
* :func:`char_ngram_jaccard` — character-level n-gram Jaccard similarity, a
  tokeniser-free / dependency-free pure function (一期 §21.14#7 style).  The
  repo's existing :func:`qai.chat.application.use_cases.discussion_intent_rules.topic_overlap_ratio`
  is a *keyword-set* Jaccard (Latin words + CJK n-grams), which is a coarser
  signal; §22A.4 specifically calls for character n-gram similarity to catch
  near-verbatim restatements, so a dedicated char-gram primitive lives here.
* :func:`score_no_new_info` — the per-turn 0..N additive score (the algorithm
  in §22A.4): the higher the score, the less new information this turn carried.

Layering: ``application/use_cases`` — pure constants + pure functions over
stdlib only.  No ports / domain / adapters / fastapi imports, so the
``layered-chat`` / ``context-isolation`` import-linter contracts hold.  The
ConvergenceController (same layer) holds the per-discussion CONSECUTIVE counter;
these functions are stateless so they stay trivially unit-testable.
"""

from __future__ import annotations

import re

from qai.chat.application.use_cases.convergence_defaults import (
    MIN_TURNS_BEFORE_END,
)

__all__ = [
    "SIMILARITY_WINDOW_TURNS",
    "NGRAM_SIZE",
    "HIGH_SIMILARITY_THRESHOLD",
    "CONSECUTIVE_NO_NEW_TURNS",
    "MIN_TURNS_BEFORE_SOFT_STOP",
    "NO_NEW_INFO_SCORE_THRESHOLD",
    "SHORT_RESPONSE_CHARS",
    "char_ngram_jaccard",
    "score_no_new_info",
]


# ── Conservative threshold constants (§22A.4) ────────────────────────────────
#: How many of the most-recent OTHER-role turns the current turn is compared
#: against for the "high similarity" signal (a small window keeps it cheap and
#: focused on the live thread, not the whole transcript).
SIMILARITY_WINDOW_TURNS = 3

#: Character n-gram window size for :func:`char_ngram_jaccard` (3 = trigrams —
#: short enough to share heavily on restatements, long enough to不被单字噪声主导).
NGRAM_SIZE = 3

#: A turn whose char-trigram Jaccard against a comparison text is at-or-above
#: this is "highly similar" (near-restatement).  0.72 is conservative — true
#: paraphrases of the SAME point routinely clear it, while genuinely new content
#: stays well below.
HIGH_SIMILARITY_THRESHOLD = 0.72

#: The detector only soft-stops after THIS many CONSECUTIVE high-score turns.
#: 2 = a single noisy/short turn can never end a discussion on its own; the
#: stall must persist (anti-误砍 guard #1).
CONSECUTIVE_NO_NEW_TURNS = 2

#: Minimum completed rounds before the SoftStop may fire at all (anti-误砍 guard
#: #2).  Reuses the SHARED minimum-rounds floor (§22A.4): the same lower bound
#: the Manager early-END (P1-step3) refuses to end before, so neither
#: convergence path can conclude a discussion prematurely.
MIN_TURNS_BEFORE_SOFT_STOP = MIN_TURNS_BEFORE_END

#: A turn must score AT-OR-ABOVE this (out of the additive signals below) to be
#: a "no new info" turn.  3-of-5 keeps a turn that's e.g. merely短 but otherwise
#: substantive (one signal) well clear of the threshold.
NO_NEW_INFO_SCORE_THRESHOLD = 3

#: Stripped responses shorter than this many characters are "too short" to plausibly
#: carry a new substantive contribution.  Conservative (small) so a terse but
#: pointed one-liner是不会单凭长度就 high-score 的(它还需另外≥2 个信号命中).
SHORT_RESPONSE_CHARS = 40


# ── New-information keyword heuristics (conservative, tokeniser-free) ─────────
# A turn that introduces a NEW claim / risk / question / decision is carrying
# new information.  These are deliberately broad bilingual (en + zh) cue sets:
# the goal is to AVOID false "no new info" positives — if ANY cue is present we
# treat the turn as plausibly substantive (the signal does NOT fire).  Missing a
# real cue only makes the detector MORE conservative on the OTHER axis (we never
# soft-stop on a single signal), so a generous cue set is the safe direction.
_NEW_INFO_KEYWORDS: frozenset[str] = frozenset(
    {
        # English — claims / proposals / decisions
        "propose",
        "suggest",
        "recommend",
        "instead",
        "alternative",
        "however",
        "but",
        "because",
        "evidence",
        "data",
        "benchmark",
        "measure",
        "tradeoff",
        "trade-off",
        "decide",
        "conclusion",
        "risk",
        "concern",
        "issue",
        "problem",
        "drawback",
        "limitation",
        "assume",
        "assumption",
        # English — questions
        "what",
        "why",
        "how",
        "when",
        "which",
        "should",
        "could",
        "consider",
        # Chinese — claims / proposals / decisions
        "提议",
        "建议",
        "推荐",
        "方案",
        "替代",
        "然而",
        "但是",
        "因为",
        "证据",
        "数据",
        "基准",
        "度量",
        "权衡",
        "取舍",
        "决定",
        "结论",
        "风险",
        "担忧",
        "问题",
        "缺陷",
        "局限",
        "假设",
        # Chinese — questions / actionable
        "为什么",
        "如何",
        "怎么",
        "是否",
        "应该",
        "考虑",
    }
)

#: A turn proposes an ACTIONABLE delta when it contains an imperative / decision
#: marker (a concrete next step, not just commentary).  Same conservative spirit
#: as ``_NEW_INFO_KEYWORDS`` — broad cue set, err toward "actionable present".
_ACTIONABLE_KEYWORDS: frozenset[str] = frozenset(
    {
        "let's",
        "lets",
        "we should",
        "we could",
        "next step",
        "action",
        "implement",
        "change",
        "add",
        "remove",
        "use",
        "switch",
        "adopt",
        "test",
        "verify",
        "应该",
        "下一步",
        "实施",
        "采用",
        "改成",
        "改为",
        "增加",
        "移除",
        "使用",
        "切换",
        "验证",
        "测试",
    }
)


# ── Pure similarity primitive ────────────────────────────────────────────────
_WS_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Casefold + collapse whitespace for similarity comparison.

    Tokeniser-free: similarity works at the character level (works for CJK and
    Latin alike, no jieba / NLTK dependency).
    """
    return _WS_RE.sub(" ", text.casefold()).strip()


def _char_ngrams(text: str, n: int) -> set[str]:
    """Return the set of length-``n`` character windows over ``text``.

    Short inputs (``len < n``) yield a single whole-string gram so a tiny text
    still compares meaningfully (never an empty set for non-empty input).
    """
    if not text:
        return set()
    if len(text) < n:
        return {text}
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def char_ngram_jaccard(a: str, b: str, n: int = NGRAM_SIZE) -> float:
    """Return the character n-gram Jaccard similarity (0..1) of ``a`` and ``b``.

    ``|grams(a) ∩ grams(b)| / |grams(a) ∪ grams(b)|`` over casefolded,
    whitespace-normalised character ``n``-grams.  Deterministic, tokeniser-free
    and Linux/Windows-portable (一期 §21.14#7 style).  Returns ``0.0`` when
    either side is empty / non-text.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return 0.0
    na = _normalise(a)
    nb = _normalise(b)
    if not na or not nb:
        return 0.0
    grams_a = _char_ngrams(na, n)
    grams_b = _char_ngrams(nb, n)
    inter = len(grams_a & grams_b)
    if inter == 0:
        return 0.0
    union = len(grams_a | grams_b)
    return inter / union if union else 0.0


def _contains_any(text: str, cues: frozenset[str]) -> bool:
    """Return True iff any cue appears in the casefolded ``text`` (substring).

    Conservative substring match (not delimited) on purpose: a CJK cue like
    ``"风险"`` has no word boundary, and a generous match keeps the detector from
    falsely flagging a substantive turn as "no new info".
    """
    lowered = text.casefold()
    return any(cue in lowered for cue in cues)


# ── The §22A.4 additive "no new info" score ──────────────────────────────────
def score_no_new_info(
    *,
    current_text: str,
    recent_texts: list[str],
    same_role_prev_text: str | None,
    manager_recently_suggested_end: bool = False,
    high_similarity_threshold: float = HIGH_SIMILARITY_THRESHOLD,
) -> tuple[int, tuple[str, ...]]:
    """Score how little NEW information ``current_text`` carried (0..5).

    Implements the §22A.4 additive algorithm (each fired signal +1).  A higher
    score means a more redundant / empty turn.  The caller (the
    ConvergenceController) decides whether the score clears
    :data:`NO_NEW_INFO_SCORE_THRESHOLD` for enough CONSECUTIVE turns past the
    minimum-rounds floor before actually soft-stopping.

    Signals (all conservative — biased toward NOT firing so we never误砍):

    * ``too_short`` — stripped length < :data:`SHORT_RESPONSE_CHARS`.
    * ``high_similarity`` — char-trigram Jaccard against the most-recent
      OTHER-role turns (``recent_texts``) OR the same role's previous turn
      reaches :data:`HIGH_SIMILARITY_THRESHOLD` (near-restatement).
    * ``no_new_keywords`` — none of the new-claim/risk/question cues present.
    * ``no_actionable_delta`` — no actionable / decision cue present.
    * ``manager_suggested_end`` — P1-step4 leaves this as an opt-in HOOK
      (defaults ``False``); the Manager early-END is already an independent
      signal, so it is normally left off here.

    Returns ``(score, fired_signal_names)`` — the names are for observability.
    An empty / blank ``current_text`` returns ``(0, ())``: a turn that produced
    no text at all is NOT scored as "stalled repetition" (it is handled by the
    normal turn flow), keeping the detector inert for the empty-input edge.
    """
    text = (current_text or "").strip()
    if not text:
        # No substantive text → do not contribute to the stall score (an empty
        # turn is not "repetition"; treating it as high-score would误砍).
        return 0, ()

    score = 0
    fired: list[str] = []

    if len(text) < SHORT_RESPONSE_CHARS:
        score += 1
        fired.append("too_short")

    best_sim = 0.0
    window = [t for t in recent_texts if isinstance(t, str) and t.strip()]
    for other in window[-SIMILARITY_WINDOW_TURNS:]:
        best_sim = max(best_sim, char_ngram_jaccard(text, other))
    if same_role_prev_text and same_role_prev_text.strip():
        best_sim = max(best_sim, char_ngram_jaccard(text, same_role_prev_text))
    if best_sim >= high_similarity_threshold:
        score += 1
        fired.append("high_similarity")

    if not _contains_any(text, _NEW_INFO_KEYWORDS):
        score += 1
        fired.append("no_new_keywords")

    if not _contains_any(text, _ACTIONABLE_KEYWORDS):
        score += 1
        fired.append("no_actionable_delta")

    if manager_recently_suggested_end:
        score += 1
        fired.append("manager_suggested_end")

    return score, tuple(fired)
