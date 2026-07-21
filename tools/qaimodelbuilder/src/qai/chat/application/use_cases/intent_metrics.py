# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Intent-classifier observability helpers (DISC-2 P2-step2 — §22A.9#2).

Pure, dependency-free helpers for the discussion intent classifier's
**observability layer**.  This module adds NOTHING to the user-visible behaviour
of a discussion: every function here is a side-effect-free pure function whose
output is only ever fed to a log record.  It exists so we can, after shipping,
aggregate from logs:

* classifier **hit / fallback / gating** rates (the §22A.9#2 immediate metrics —
  emitted by ``orchestrate_discussion._maybe_refine_intent``); and
* **post-hoc correction proxies** — cheap, conservative cross-turn heuristics
  that flag a *possible* mis-classification on the FOLLOWING turn:

  - ``possible_underclassification`` — the prior turn was judged *lightweight*
    (social / ack / scoped follow-up, NOT full) but THIS turn the user asks to
    expand / go deeper / "why" → maybe we under-classified last turn.
  - ``possible_overclassification`` — the prior turn was judged *full*
    (deep_task) but THIS turn the user says "keep it short / don't expand /
    too long" → maybe we over-classified last turn.
  - ``target_role_correction`` — the prior turn resolved some ``target_roles``
    but THIS turn the user corrects the addressee ("not A, ask B" / "应该问 B")
    → maybe we mis-routed the target last turn.

Privacy (§22A.9#2 — "日志只存 message_length / language / signal tags / intent
result / hashed conv id，不长期存原文"):

* :func:`detect_language` and :func:`hash_conv_id` produce the ONLY conversation-
  derived fields ever logged — a coarse ``"zh"``/``"en"`` tag and a truncated
  SHA-256 of the conversation id.  Neither returns or embeds the raw message.
* The post-hoc detectors return a small frozen result carrying ONLY a boolean +
  the matched **category tag** (never the matched substring or the raw text), so
  a caller that logs the result cannot leak message content.

Design decisions (recorded per the task brief):

* **Post-hoc detection is ALWAYS-ON and FLAG-INDEPENDENT.**  Unlike the LLM
  classifier (gated behind ``intent_classifier_enabled``), the proxy detectors
  are a zero-cost keyword scan that observe the quality of the WHOLE intent
  pipeline (heuristic + optional LLM).  They compare THIS turn's user message
  against the PRIOR turn's *recorded* classification, which exists regardless of
  whether the LLM was ever consulted.  Running them unconditionally gives us a
  complete picture of classification quality even in the default flag-off
  deployment — and costs nothing but a substring scan.
* **Conservative — prefer false negatives over false positives.**  These are
  only *proxy* signals for offline review, so the keyword sets are deliberately
  narrow: a missed correction is harmless (one fewer data point), a spurious one
  pollutes the metric.  Hence small, unambiguous bilingual (en + zh) keyword
  sets and AND-style guards (e.g. role-correction needs BOTH a negation cue and
  an addressing cue).

Layering: ``application/use_cases`` — depends only on stdlib + the sibling
``discussion_intent`` vocabulary types.  No ports, domain, adapters, apps or
interfaces, so ``layered-chat`` / ``context-isolation`` hold and every helper is
trivially unit-testable in isolation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from qai.chat.application.use_cases.discussion_intent import DiscussionIntent

__all__ = [
    "detect_language",
    "hash_conv_id",
    "PostHocSignal",
    "detect_posthoc_correction",
    "LIGHTWEIGHT_INTENTS",
    "FULL_INTENTS",
]


# ---------------------------------------------------------------------------
# De-identification helpers (the ONLY conversation-derived log fields)
# ---------------------------------------------------------------------------
#: Matches any CJK Unified Ideograph (+ common extensions) — used only to pick a
#: coarse ``"zh"`` vs ``"en"`` language tag for the log, never to parse content.
_CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002a6df]"
)


def detect_language(text: str) -> str:
    """Return a coarse ``"zh"`` / ``"en"`` language tag for ``text``.

    A deliberately trivial heuristic (§22A.9#2 "简单启发式：含 CJK→zh，否则
    en"): if the message contains ANY CJK ideograph it is tagged ``"zh"``,
    otherwise ``"en"``.  This is a privacy-preserving log field — it conveys
    only the gross language bucket, NEVER the message itself.  A non-``str`` or
    empty input yields ``"en"`` (the neutral default).
    """
    if not isinstance(text, str) or not text:
        return "en"
    return "zh" if _CJK_RE.search(text) else "en"


def hash_conv_id(conv_id: str) -> str:
    """Return the first 12 hex chars of SHA-256(``conv_id``).

    Lets log aggregation correlate the turns of one conversation WITHOUT storing
    the plaintext conversation id (§22A.9#2 "hashed conv id，不存明文").  The
    truncation keeps the field short; SHA-256 makes it deterministic (same id →
    same hash, so cross-turn correlation works) and one-way (cannot be reversed
    to the id).  A non-``str`` / empty input hashes the empty string (stable).
    """
    raw = conv_id if isinstance(conv_id, str) else ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Post-hoc correction proxy (cross-turn, always-on, conservative)
# ---------------------------------------------------------------------------
#: Intents the prior turn must carry to be a *lightweight* classification — an
#: under-classification proxy fires only when the prior turn was one of these.
LIGHTWEIGHT_INTENTS: frozenset[DiscussionIntent] = frozenset(
    {"social", "ack", "follow_up", "directed_follow_up"}
)
#: Intents the prior turn must carry to be a *full* classification — an
#: over-classification proxy fires only when the prior turn was one of these.
FULL_INTENTS: frozenset[DiscussionIntent] = frozenset({"deep_task"})

#: "Expand / go deeper / why" cues → user wants MORE than the lightweight reply
#: they got last turn.  Conservative bilingual set; all matched case-folded as
#: substrings of the normalised message.
_EXPAND_CUES: tuple[str, ...] = (
    # zh
    "展开",
    "详细",
    "详细说",
    "具体说",
    "说说",
    "深入",
    "为什么",
    "为啥",
    "讲讲",
    "再说说",
    "多说",
    # en
    "expand",
    "elaborate",
    "in detail",
    "more detail",
    "tell me more",
    "go deeper",
    "why",
    "explain more",
)
#: "Keep it short / don't expand / too long" cues → user wanted LESS than the
#: full discussion they got last turn.
_SHORTEN_CUES: tuple[str, ...] = (
    # zh
    "简单说",
    "简单点",
    "不用展开",
    "别展开",
    "太长",
    "简短",
    "简要",
    "长话短说",
    "说重点",
    "简单一点",
    # en
    "keep it short",
    "too long",
    "shorter",
    "be brief",
    "briefly",
    "in short",
    "tldr",
    "tl;dr",
    "don't expand",
    "dont expand",
    "no need to expand",
)
#: Negation cues for a target-role correction ("not A, …").  Paired (AND) with an
#: addressing cue below so a bare "不是" never fires the proxy.
_ROLE_NEGATION_CUES: tuple[str, ...] = (
    # zh
    "不是",
    "不该问",
    "不要问",
    # en
    "not ",
    "no, ",
    "instead of",
)
#: Addressing cues for a target-role correction ("…ask B" / "let B").  Paired
#: (AND) with a negation cue so only an explicit re-direction fires.
_ROLE_ADDRESS_CUES: tuple[str, ...] = (
    # zh
    "应该问",
    "去问",
    "问问",
    "让",
    "找",
    # en
    "ask ",
    "let ",
    "should ask",
)


@dataclass(frozen=True, slots=True)
class PostHocSignal:
    """A cross-turn post-hoc correction proxy result (privacy-safe).

    Carries ONLY a category tag — never the matched substring or the raw user
    text — so a caller may log it without leaking message content.

    * ``signal`` — one of ``"possible_underclassification"`` /
      ``"possible_overclassification"`` / ``"target_role_correction"`` /
      ``"none"`` (no proxy fired).
    * ``fired`` — convenience flag = ``signal != "none"``.
    """

    signal: str
    fired: bool


_NO_SIGNAL = PostHocSignal(signal="none", fired=False)


def _contains_any(haystack: str, needles: tuple[str, ...]) -> bool:
    """Case-folded substring scan — ``True`` if any needle is in ``haystack``."""
    return any(needle in haystack for needle in needles)


def detect_posthoc_correction(
    *,
    message: str,
    prior_intent: DiscussionIntent | None,
    prior_was_full: bool,
    prior_target_roles: tuple[str, ...] = (),
) -> PostHocSignal:
    """Detect a *possible* prior-turn mis-classification from THIS user message.

    A pure, conservative, always-on cross-turn proxy (§22A.9#2).  Compares the
    CURRENT user ``message`` against the PRIOR turn's recorded classification
    (``prior_intent`` / ``prior_was_full`` / ``prior_target_roles``) and returns
    at most ONE :class:`PostHocSignal`.  This NEVER changes any behaviour — the
    result is only ever logged.

    Detection order (first match wins; mutually exclusive by design):

    1. ``target_role_correction`` — prior turn had target roles AND this message
       carries BOTH a negation cue and an addressing cue (a deliberate
       re-direction like "不是 A，应该问 B" / "not A, ask B").  Checked first
       because a re-direction is the most specific correction.
    2. ``possible_underclassification`` — prior intent was *lightweight* and this
       message asks to expand / go deeper / why.
    3. ``possible_overclassification`` — prior turn was *full* and this message
       asks to keep it short / not expand.

    Returns :data:`PostHocSignal` with ``signal == "none"`` when nothing fires
    (the common case) or when ``prior_intent is None`` (no prior turn to compare
    against).  Conservative: prefers a miss over a false positive.
    """
    if prior_intent is None or not isinstance(message, str) or not message:
        return _NO_SIGNAL
    text = message.strip().casefold()
    if not text:
        return _NO_SIGNAL

    # 1) Target-role correction (most specific) — requires BOTH cues so a bare
    #    "不是" / "not sure" never misfires (conservative AND-guard).
    if (
        prior_target_roles
        and _contains_any(text, _ROLE_NEGATION_CUES)
        and _contains_any(text, _ROLE_ADDRESS_CUES)
    ):
        return PostHocSignal(signal="target_role_correction", fired=True)

    # 2) Under-classification — prior was lightweight, user now wants more.
    if prior_intent in LIGHTWEIGHT_INTENTS and _contains_any(text, _EXPAND_CUES):
        return PostHocSignal(
            signal="possible_underclassification", fired=True
        )

    # 3) Over-classification — prior was full, user now wants less.
    if (
        prior_was_full
        and prior_intent in FULL_INTENTS
        and _contains_any(text, _SHORTEN_CUES)
    ):
        return PostHocSignal(signal="possible_overclassification", fired=True)

    return _NO_SIGNAL
