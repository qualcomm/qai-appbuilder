# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Discussion Intent Router (§21.3) — heuristic, dependency-free classifier.

Layer 1 of the four-layer discussion orchestration (§21.2): given the user's
latest message + light conversation context, decide WHICH of the five public
discussion intents it carries (plus an internal subtype the Policy Planner
consumes).  The router is a **pure function** with NO IO and NO LLM call for the
MVP (§21.13): every decision is reproducible from its inputs, so it is trivially
unit-testable (§21.3.1#5) and adds zero latency to the common "Hi" path.

Public intents (stable contract written to ``meta`` — §21.3):
``social`` | ``ack`` | ``follow_up`` | ``directed_follow_up`` | ``deep_task``.

Internal subtypes (Planner-only, never surfaced to the wire / telemetry):
``social_greeting`` | ``thanks_or_closing`` | ``ack_passive`` |
``continue_request`` | ``directed_deep_task`` | ``none``.

Heuristic ladder (§21.3, all rules — no LLM in the MVP):

* **L1 strong rules** — a normalised greeting/thanks/ack dictionary hit on a
  short, ``?``-free, ``@``-free, task-verb-free message → ``social`` / ``ack``.
* **L2 medium rules** — an ``@mention`` → directed (deep vs follow-up by
  task-verb presence); a task verb or ``?`` → ``deep_task`` / ``follow_up``; a
  continue-word hit → ``ack.continue_request``; high topic overlap with the
  previous turn → ``follow_up``.
* **L3 grey-zone conservative fallback** — when short and ambiguous: ``@`` →
  directed; ``?`` / question-word / task-verb → ``follow_up``; ``awaiting_user``
  + short → ``follow_up``; ``active_discussion`` + continue-word → continuation;
  otherwise → ``social`` / ``ack``.  **Never defaults UP to ``full``** — the
  whole point is to STOP over-discussion, so ambiguity degrades (§21.11).

A grey-zone **LLM classifier port is reserved but OFF** for the MVP (§21.3,
§21.8 P1): the router accepts an optional ``classifier`` callable that is simply
not wired today (``None``).  The seam exists so phase 2 can add it without
touching call sites.

Layering: ``application/use_cases`` — depends only on the sibling rule pack +
stdlib.  No ports, no domain, no adapters, so ``layered-chat`` /
``context-isolation`` hold and the router is reusable from tests directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from qai.chat.application.use_cases import discussion_intent_rules as rules

__all__ = [
    "DiscussionIntent",
    "DiscussionIntentSubtype",
    "DiscussionState",
    "IntentResult",
    "IntentHeuristicResult",
    "IntentClassifierPort",
    "classify_intent",
    "diagnose_intent",
]


# ---------------------------------------------------------------------------
# Public vocabulary (stable contracts)
# ---------------------------------------------------------------------------
DiscussionIntent = Literal[
    "social",
    "ack",
    "follow_up",
    "directed_follow_up",
    "deep_task",
]
DiscussionIntentSubtype = Literal[
    "social_greeting",
    "thanks_or_closing",
    "ack_passive",
    "continue_request",
    "directed_deep_task",
    "implement",
    "none",
]
#: The four persisted discussion states (§21.6).  ``social`` / ``wrap_up`` are
#: intentionally NOT states (they are intents / framing modes) so "Hi Hi Hi" or
#: "继续 继续" never thrash a persisted state.
DiscussionState = Literal[
    "idle",
    "active_discussion",
    "awaiting_user",
    "closed",
]


@dataclass(frozen=True, slots=True)
class IntentResult:
    """The router's verdict for one user message (§21.3 output contract).

    * ``intent`` — the public 5-class intent (written to ``meta``);
    * ``subtype`` — the internal subtype the Policy Planner consumes (never
      surfaced to the wire / telemetry);
    * ``confidence`` — 0..1 heuristic confidence (informational; grey-zone
      verdicts carry a lower number).  Not a gate in the MVP;
    * ``target_roles`` — the resolved ``@mention`` display names (verbatim from
      the caller's :func:`parse_mentions`), preserved order;
    * ``needs_full_discussion`` — convenience flag = ``intent == "deep_task"``.
    * ``focus_terms`` — DISC-2 §22A.6 P3-b ("light follow-up focus hints"): the
      deterministic content keyword(s) extracted from a SCOPED follow-up
      ("那安全方面呢？" → ``("安全",)``), so the orchestrator can frame the
      scoped turn with "focus only on this topic".  Populated ONLY for the
      scoped follow-up intents (``follow_up`` / ``directed_follow_up``); empty
      ``()`` for everything else (and whenever no content term survives) — an
      empty tuple injects NO hint, so framing is byte-for-byte unchanged
      (§3.1 additive; zero-regression).
    * ``route_kind`` — DISC-1 §22.5/§22.7 internal execution-decision marker:
      ``"default"`` (every existing path) | ``"directed_implement"`` (an
      ``@mention`` + a窄 "implement/落地" verb in a NON-question message).  It is
      a PRIVATE downstream-routing hint ONLY: the public ``intent`` / ``subtype``
      contract NEVER depends on it, and DISC-1 step1 still routes
      ``"directed_implement"`` through the EXISTING ``directed_deep_task`` policy
      branch byte-for-byte (the marker merely drives one extra audit log + the
      step3 tool編排 that is NOT wired yet — zero behaviour change in step1/step2).
      Defaults to ``"default"`` so every existing construction stays unchanged
      (§3.1 additive; frozen+slots compatible).
    """

    intent: DiscussionIntent
    subtype: DiscussionIntentSubtype
    confidence: float
    target_roles: tuple[str, ...]
    needs_full_discussion: bool
    focus_terms: tuple[str, ...] = ()
    route_kind: str = "default"


@dataclass(frozen=True, slots=True)
class IntentHeuristicResult:
    """Structured heuristic diagnosis used to gate the grey-zone LLM (§22A.5).

    Produced by :func:`diagnose_intent` — a pure, side-effect-free companion to
    :func:`classify_intent` that exposes WHY the heuristic landed where it did,
    so the orchestrator can decide whether the grey-zone LLM classifier is worth
    a call (DISC-2 P2-step1).  Copilot's insight (§22A.5): do NOT gate on a bare
    ``confidence < threshold`` — emit a structured diagnosis and only call the
    LLM for the four genuine grey-zone shapes.

    Fields:

    * ``intent`` — the public 5-class intent, SAME value :func:`classify_intent`
      returns for the same inputs (one口径).
    * ``confidence`` — the heuristic confidence, also identical to
      :func:`classify_intent`.
    * ``signals`` — the diagnostic signal names that fired (e.g. ``"short"`` /
      ``"question_mark"`` / ``"task_verb"`` / ``"mention"`` / ``"continue_word"``
      / ``"topic_overlap"``).  The orchestrator's "strong signal" gate (§21.11)
      reads ``"task_verb"`` / ``"mention"`` out of this tuple to decide whether
      an LLM verdict may escalate to full.
    * ``ambiguity_reasons`` — which of the four §22A.5 grey-zone shapes were hit
      (``"short_elliptical"`` / ``"multi_intent"`` /
      ``"context_dependent_followup"`` / ``"low_conf_escalation"``).
    * ``eligible_for_llm_fallback`` — ``True`` ONLY when the message is a genuine
      grey-zone case worth an LLM call (heuristic confidence in the grey band +
      at least one ambiguity reason + no clear direct-out signal).
    """

    intent: DiscussionIntent
    confidence: float
    signals: tuple[str, ...]
    ambiguity_reasons: tuple[str, ...]
    eligible_for_llm_fallback: bool


@runtime_checkable
class IntentClassifierPort(Protocol):
    """Grey-zone LLM classifier seam (DISC-2 P2-step1 — §22A.5).

    A low-temperature, small-schema LLM classifier for the ambiguous middle.
    The orchestrator calls it ONLY when (a) the ``intent_classifier_enabled``
    flag is on, (b) a concrete classifier is wired, and (c) the heuristic marked
    the message ``eligible_for_llm_fallback`` (a genuine grey-zone shape).  When
    any of those is false the orchestrator uses the pure-heuristic verdict
    verbatim and NEVER reaches this port — so a flag-off / unwired deployment
    behaves exactly like the heuristic-only implementation.

    Implementations MUST be side-effect free w.r.t. the conversation and MUST
    NOT raise — a failure / timeout / unparseable reply degrades to the
    heuristic verdict (the orchestrator wraps the call in ``asyncio.wait_for`` +
    a broad ``except``; the impl returning ``None`` is the in-band "keep
    heuristic" signal).
    """

    async def classify(
        self,
        *,
        message: str,
        state: DiscussionState,
        awaiting_user: bool,
        previous_user_text: str | None = None,
        mentions: tuple[str, ...] = (),
        model_hint: str | None = None,
        timeout_ms: int = 2000,
        heuristic: IntentHeuristicResult | None = None,
    ) -> IntentResult | None:
        """Return a refined verdict, or ``None`` to keep the heuristic one.

        Args mirror :func:`classify_intent`'s grey-zone inputs plus the
        ``heuristic`` diagnosis (so the impl can prompt the LLM with the
        heuristic's best guess) and a per-call ``model_hint`` / ``timeout_ms``.
        """
        ...


# ---------------------------------------------------------------------------
# Heuristic classifier (pure)
# ---------------------------------------------------------------------------
def _has_question_mark(text: str) -> bool:
    return "?" in text or "？" in text


def classify_intent(
    *,
    message: str,
    mentions: list[str] | None = None,
    state: DiscussionState = "idle",
    awaiting_user: bool = False,
    previous_user_text: str | None = None,
    locale: str | None = None,
    classifier: IntentClassifierPort | None = None,
) -> IntentResult:
    """Classify ``message`` into a discussion intent + subtype (§21.3).

    Pure + deterministic (MVP): the same inputs always yield the same verdict.

    Args:
        message: the user's latest raw message text.
        mentions: the ``@mention`` display names already parsed by the caller
            (via ``orchestrate_discussion.parse_mentions``) — passed in rather
            than re-parsed so the router stays free of that module (no circular
            import) and the single mention-parse rule is reused.
        state: the conversation's current ``discussion_state`` (§21.6).
        awaiting_user: whether the previous orchestrated turn ended awaiting the
            user (a short reply then more likely CONTINUES than greets).
        previous_user_text: the prior user message, for topic-overlap continuity.
        locale: best-effort UI locale ("en" / "zh-CN" / "zh-TW"); the rule pack
            still scans every locale so this is only a priority hint.
        classifier: reserved grey-zone LLM port — OFF in the MVP (``None``).

    Returns:
        An :class:`IntentResult`.  Grey-zone verdicts NEVER escalate to
        ``deep_task`` (ambiguity degrades — §21.11).
    """
    target_roles = tuple(mentions or ())
    normalized = rules.normalize_message(message)
    raw = message if isinstance(message, str) else ""

    # DISC-2 §22A.6 P3-b — deterministic focus keyword(s) for a scoped
    # follow-up.  Computed once here from the heuristic message (no分词, no IO)
    # and attached ONLY to the scoped follow-up return branches below
    # (``follow_up`` / ``directed_follow_up``).  Empty when nothing
    # content-bearing survives → no hint injected downstream (zero-regression).
    focus_terms = rules.extract_focus_terms(message, locale=locale)

    has_q = _has_question_mark(raw)
    has_task_verb = rules.contains_any(normalized, rules.TASK_VERB_TERMS, locale)
    has_follow_hint = rules.contains_any(
        normalized, rules.FOLLOW_UP_HINT_TERMS, locale
    )
    has_question_word = rules.contains_any(
        normalized, rules.QUESTION_WORD_TERMS, locale
    )
    is_short = len(normalized) <= rules.SHORT_MESSAGE_MAX_CHARS

    is_continue = rules.matches_exact(
        normalized, rules.CONTINUE_REQUEST_TERMS, locale
    ) or rules.contains_term(normalized, rules.CONTINUE_REQUEST_TERMS, locale)
    is_thanks = rules.matches_exact(
        normalized, rules.THANKS_OR_CLOSING_TERMS, locale
    ) or rules.contains_term(normalized, rules.THANKS_OR_CLOSING_TERMS, locale)
    is_greeting = rules.matches_exact(
        normalized, rules.SOCIAL_GREETING_TERMS, locale
    ) or rules.contains_term(normalized, rules.SOCIAL_GREETING_TERMS, locale)
    is_ack = rules.matches_exact(normalized, rules.ACK_PASSIVE_TERMS, locale)

    # -- L2-a: @mention → directed (strict; deep vs follow-up by task verb) ----
    # An @mention is a hard "address these roles" signal regardless of length.
    # A directed message with a real task verb is a directed DEEP task (let the
    # mentioned role do a full analysis); otherwise it is a directed follow-up
    # (a single scoped reply).  §21.4 deep_task+@mention row, §21.14#3.
    if target_roles:
        if has_task_verb:
            # DISC-1 §22.5/§22.7 step1 — implement sub-routing.  When the directed
            # task carries a窄 "implement/落地" verb AND is NOT a question (no "?",
            # no question word), mark it ``subtype="implement"`` +
            # ``route_kind="directed_implement"``.  The ambiguity guard keeps a
            # question ("@dev 这个能实现吗？") as a plain ``directed_deep_task``
            # discussion (NOT an implement request).  Crucially the public
            # ``intent`` STAYS ``"deep_task"`` and ``needs_full_discussion`` STAYS
            # ``True`` — downstream ``plan_policy`` handles ``"implement"`` through
            # the SAME ``directed_deep_task`` branch byte-for-byte (DISC-1 step1
            # is side-effect free; the real tool編排 lands in step3).
            has_implement_verb = rules.contains_any(
                normalized, rules.IMPLEMENT_VERB_TERMS, locale
            )
            if has_implement_verb and not has_q and not has_question_word:
                return IntentResult(
                    intent="deep_task",
                    subtype="implement",
                    confidence=0.9,
                    target_roles=target_roles,
                    needs_full_discussion=True,
                    focus_terms=focus_terms,
                    route_kind="directed_implement",
                )
            return IntentResult(
                intent="deep_task",
                subtype="directed_deep_task",
                confidence=0.9,
                target_roles=target_roles,
                needs_full_discussion=True,
            )
        return IntentResult(
            intent="directed_follow_up",
            subtype="none",
            confidence=0.85,
            target_roles=target_roles,
            needs_full_discussion=False,
            focus_terms=focus_terms,
        )

    # -- L2-b: continue request (must beat passive-ack; §21.3) -----------------
    # "继续 / continue" combined with an active/awaiting state means EXPAND, not
    # stop.  But a continue word riding a real question ("继续讲讲为什么？") must
    # not be downgraded — the question/task signal below would already have
    # promoted it; here we only treat a continue word WITHOUT strong task/?
    # signal as a continuation request.
    if is_continue and not has_task_verb:
        # When the message ALSO clearly asks a question, treat it as a follow-up
        # (a scoped, on-topic answer) rather than a bare "go on".
        subtype: DiscussionIntentSubtype = "continue_request"
        return IntentResult(
            intent="ack",
            subtype=subtype,
            confidence=0.8 if not has_q else 0.65,
            target_roles=(),
            needs_full_discussion=False,
        )

    # -- L1 strong: short pure social / ack (no question, no task, no mention) -
    # Only when the message is SHORT and carries no substantive signal does a
    # greeting/thanks/ack dictionary hit win outright.  This stops "Hi" from
    # ever opening a full discussion (§21.14#5), while "Hi, can you analyse X?"
    # falls through (has_q / has_task_verb) to a real follow_up/deep_task.
    substantive = has_q or has_task_verb or has_question_word
    if is_short and not substantive:
        if is_thanks:
            return IntentResult(
                intent="social",
                subtype="thanks_or_closing",
                confidence=0.9,
                target_roles=(),
                needs_full_discussion=False,
            )
        if is_greeting:
            return IntentResult(
                intent="social",
                subtype="social_greeting",
                confidence=0.9,
                target_roles=(),
                needs_full_discussion=False,
            )
        if is_ack:
            return IntentResult(
                intent="ack",
                subtype="ack_passive",
                confidence=0.85,
                target_roles=(),
                needs_full_discussion=False,
            )

    # -- L2-c: substantive task / question -------------------------------------
    # A SHORT interrogative (question mark or question word) is a FOLLOW-UP even
    # when it mentions a task-verb noun: "为什么要这样设计？" asks ABOUT prior
    # work, it does not request a fresh full task.  Per §21.11 a grey-zone
    # short question degrades to scoped (follow_up), never escalates to full —
    # so the question signal wins over an incidental task verb when the message
    # is short.  A LONG message with a task verb is a real "do work" request.
    is_short_question = (has_q or has_question_word) and is_short
    # A task verb is the strongest "do real work" signal → deep_task (full),
    # UNLESS it is just a short question about existing work (handled above).
    if has_task_verb and not is_short_question:
        return IntentResult(
            intent="deep_task",
            subtype="none",
            confidence=0.85,
            target_roles=(),
            needs_full_discussion=True,
        )
    # A question (mark or interrogative word) or an explicit follow-up hint is a
    # FOLLOW-UP — a scoped, on-topic answer, NOT a fresh full discussion (§21.4).
    if has_q or has_question_word or has_follow_hint:
        return IntentResult(
            intent="follow_up",
            subtype="none",
            confidence=0.7,
            target_roles=(),
            needs_full_discussion=False,
            focus_terms=focus_terms,
        )

    # -- L2-d: topic-overlap continuity ----------------------------------------
    # No explicit question/verb, but strongly on the prior topic → follow_up
    # (the user is continuing the thread with a statement). §21.14#7-①.
    #
    # State-Truth-First guard (AGENTS.md §🔴): only treat topic overlap as a
    # follow-up when there IS an active/awaiting thread to continue.  In ``idle``
    # / ``closed`` state there is no live discussion to follow up on, so a
    # ``previous_user_text`` here is necessarily STALE (e.g. the front-end
    # "Clear" button cleared the visible buffer but the backend conversation —
    # and its old messages — survived, leaving residual prior text).  Without
    # this guard a fresh greeting after a clear gets mis-routed to ``follow_up``
    # / ``followup_mode``, producing a self-contradictory prompt ("only respond
    # to the user's follow-up, continue the existing discussion") against a
    # stale transcript — which the cloud proxy answers with an empty 200,
    # surfacing as ``speaker_error``.  Topic overlap may only escalate while a
    # discussion is genuinely live.
    if (
        state not in ("idle", "closed")
        and previous_user_text
        and rules.topic_overlap_ratio(message, previous_user_text)
        >= rules.TOPIC_OVERLAP_THRESHOLD
    ):
        return IntentResult(
            intent="follow_up",
            subtype="none",
            confidence=0.6,
            target_roles=(),
            needs_full_discussion=False,
            focus_terms=focus_terms,
        )

    # -- L3 grey-zone conservative fallback (never escalate to full) -----------
    # Short thanks/greeting/ack even if not caught above (e.g. a slightly long
    # thanks) → social/ack so we still wrap up gracefully.
    if is_thanks:
        return IntentResult(
            intent="social",
            subtype="thanks_or_closing",
            confidence=0.55,
            target_roles=(),
            needs_full_discussion=False,
        )
    if is_greeting:
        return IntentResult(
            intent="social",
            subtype="social_greeting",
            confidence=0.55,
            target_roles=(),
            needs_full_discussion=False,
        )
    if is_ack:
        return IntentResult(
            intent="ack",
            subtype="ack_passive",
            confidence=0.5,
            target_roles=(),
            needs_full_discussion=False,
        )
    # An awaiting_user short reply that is none of the above is most likely a
    # terse continuation/clarification → follow_up (scoped), not a greeting.
    if awaiting_user and is_short:
        return IntentResult(
            intent="follow_up",
            subtype="none",
            confidence=0.45,
            target_roles=(),
            needs_full_discussion=False,
            focus_terms=focus_terms,
        )
    # In an active discussion, a short ambiguous line is most likely a
    # continuation cue → ack.continue_request (scoped continuation), not full.
    if state == "active_discussion" and is_short:
        return IntentResult(
            intent="ack",
            subtype="continue_request",
            confidence=0.4,
            target_roles=(),
            needs_full_discussion=False,
        )
    # idle / closed + a longer, non-social, non-question, off-topic message:
    # this is the user opening a NEW substantive line of inquiry.  With no task
    # verb / question it is still ambiguous; per §21.11 we DO NOT escalate a
    # grey-zone message to full — but a non-short, on-no-prior-topic opener in
    # idle/closed is the legitimate "start a discussion" case, so a deep_task is
    # only chosen when the message is NOT short (a real paragraph), else we keep
    # it scoped.  This keeps "一句 Hi" tiny while letting a real opening prompt
    # start the discussion.
    if not is_short:
        return IntentResult(
            intent="deep_task",
            subtype="none",
            confidence=0.45,
            target_roles=(),
            needs_full_discussion=True,
        )
    # Final safety net: a short, signal-free message → social greeting
    # (lightweight, single brief reply).  Never full.
    return IntentResult(
        intent="social",
        subtype="social_greeting",
        confidence=0.35,
        target_roles=(),
        needs_full_discussion=False,
    )


# ---------------------------------------------------------------------------
# Structured grey-zone diagnosis (pure) — gates the optional LLM (§22A.5)
# ---------------------------------------------------------------------------
#: Heuristic confidence at/below which a verdict is "grey enough" that an LLM
#: classifier MIGHT improve it (necessary, not sufficient — an ambiguity reason
#: must also fire and the message must not be a clear direct-out).
#:
#: NOTE on the band width (§22A.5 faithfulness): the design's prose mentions the
#: L3 fallback band (``confidence <= 0.55``), but its four worked examples —
#: "那安全呢？" / "那成本方面呢？" / "说得不错，那继续看安全问题" — are SHORT
#: elliptical follow-ups that the heuristic L2-c branch routes to ``follow_up``
#: at confidence **0.7** (a question signal on a short message), and the
#: topic-overlap branch at **0.6**.  Capping eligibility at 0.55 would make those
#: very examples NEVER eligible — i.e. the LLM path would be dead code for the
#: cases §22A.5 explicitly targets.  To stay faithful to the design's INTENT
#: (disambiguate exactly those short context-dependent follow-ups) the
#: eligibility band is 0.75-inclusive (用户 2026-06-24 拍板，比文档草稿的 0.55 / 我
#: 先前的 0.7 略放宽一档，更稳妥地覆盖灰区的短追问，避免漏判).  It still excludes the
#: heuristic's CONFIDENT verdicts: a clear ``@mention`` directed reply (0.85/0.9),
#: a clear long task (``deep_task`` 0.85, also a direct-out), and the L1 strong
#: social/ack/thanks hits (0.85-0.9).  A genuine "do work" request therefore never
#: wastes an LLM call; only the ambiguous middle does.
_GREY_ZONE_CONFIDENCE_MAX = 0.75


def diagnose_intent(
    *,
    message: str,
    mentions: list[str] | None = None,
    state: DiscussionState = "idle",
    awaiting_user: bool = False,
    previous_user_text: str | None = None,
    locale: str | None = None,
) -> IntentHeuristicResult:
    """Diagnose ``message`` into a structured heuristic result (§22A.5).

    Pure + deterministic + side-effect free.  Recomputes the SAME diagnostic
    booleans :func:`classify_intent` uses (so ``signals`` is in lock-step with
    the verdict) and reuses :func:`classify_intent` for ``intent`` /
    ``confidence`` (one口径, zero drift).  The orchestrator calls this once per
    message (alongside ``classify_intent``) and consults
    ``eligible_for_llm_fallback`` to decide whether the grey-zone LLM classifier
    is worth a call.

    Eligibility (``eligible_for_llm_fallback == True``) requires ALL of:

    * the heuristic verdict is in the grey band
      (``confidence <= _GREY_ZONE_CONFIDENCE_MAX``);
    * at least one §22A.5 ambiguity reason fired;
    * NO clear direct-out signal — no ``@mention``, no clear stop/cancel, no
      clear strong task verb (those are规则直出, §22A.5 "不触发 LLM" list).
    """
    target_roles = tuple(mentions or ())
    normalized = rules.normalize_message(message)
    raw = message if isinstance(message, str) else ""

    has_q = _has_question_mark(raw)
    has_task_verb = rules.contains_any(normalized, rules.TASK_VERB_TERMS, locale)
    has_follow_hint = rules.contains_any(
        normalized, rules.FOLLOW_UP_HINT_TERMS, locale
    )
    has_question_word = rules.contains_any(
        normalized, rules.QUESTION_WORD_TERMS, locale
    )
    is_short = len(normalized) <= rules.SHORT_MESSAGE_MAX_CHARS
    is_continue = rules.matches_exact(
        normalized, rules.CONTINUE_REQUEST_TERMS, locale
    ) or rules.contains_term(normalized, rules.CONTINUE_REQUEST_TERMS, locale)
    is_thanks = rules.matches_exact(
        normalized, rules.THANKS_OR_CLOSING_TERMS, locale
    ) or rules.contains_term(normalized, rules.THANKS_OR_CLOSING_TERMS, locale)
    is_greeting = rules.matches_exact(
        normalized, rules.SOCIAL_GREETING_TERMS, locale
    ) or rules.contains_term(normalized, rules.SOCIAL_GREETING_TERMS, locale)
    is_ack = rules.matches_exact(normalized, rules.ACK_PASSIVE_TERMS, locale)
    has_topic_overlap = bool(
        state not in ("idle", "closed")
        and previous_user_text
        and rules.topic_overlap_ratio(message, previous_user_text)
        >= rules.TOPIC_OVERLAP_THRESHOLD
    )

    # -- signals (named heuristic features that fired) -------------------------
    signals: list[str] = []
    if target_roles:
        signals.append("mention")
    if has_task_verb:
        signals.append("task_verb")
    if has_q:
        signals.append("question_mark")
    if has_question_word:
        signals.append("question_word")
    if has_follow_hint:
        signals.append("follow_hint")
    if is_short:
        signals.append("short")
    if is_continue:
        signals.append("continue_word")
    if is_thanks:
        signals.append("thanks")
    if is_greeting:
        signals.append("greeting")
    if is_ack:
        signals.append("ack")
    if has_topic_overlap:
        signals.append("topic_overlap")

    # -- the verdict (delegated — one口径 with classify_intent) -----------------
    verdict = classify_intent(
        message=message,
        mentions=mentions,
        state=state,
        awaiting_user=awaiting_user,
        previous_user_text=previous_user_text,
        locale=locale,
    )

    # -- ambiguity reasons (§22A.5 four grey-zone shapes) ----------------------
    # A reference / ellipsis / topic word with no clear task verb on a tiny
    # message → "this one?"/"那安全呢？" style.
    has_reference_word = (
        has_question_word or has_follow_hint or has_topic_overlap
    )
    ambiguity_reasons: list[str] = []
    # 1) 极短省略句: short + no task verb + carries a reference/topic/question cue.
    if (
        is_short
        and not has_task_verb
        and (has_reference_word or has_q)
    ):
        ambiguity_reasons.append("short_elliptical")
    # 2) 同时命中多个 intent: a continue/ack/social cue co-occurring with a
    #    follow-up / question cue ("说得不错，那继续看安全问题").
    soft_intent_cue = is_continue or is_thanks or is_greeting or is_ack
    followup_cue = has_q or has_question_word or has_follow_hint
    if soft_intent_cue and followup_cue and not target_roles:
        ambiguity_reasons.append("multi_intent")
    # 3) 有上下文依赖的 follow-up: a question / follow-hint that depends on prior
    #    context to judge scoped-vs-full ("那成本方面呢？"). Needs a live thread
    #    OR an awaiting-user state to be context-dependent (else it is a fresh
    #    opener, handled by the heuristic directly).
    if (
        (has_q or has_question_word or has_follow_hint)
        and not has_task_verb
        and not target_roles
        and (state not in ("idle", "closed") or awaiting_user or has_topic_overlap)
    ):
        ambiguity_reasons.append("context_dependent_followup")
    # 4) 低置信但影响成本的升级决策: the grey-zone escalation branch ("分析一下
    #    看看") — a non-short, non-social opener the heuristic promoted to
    #    deep_task at low confidence, or any deep_task verdict still in the grey
    #    band.  This is where an LLM might (or might not) confirm a full task.
    if (
        verdict.intent == "deep_task"
        and verdict.confidence <= _GREY_ZONE_CONFIDENCE_MAX
    ):
        ambiguity_reasons.append("low_conf_escalation")

    # -- eligibility gate ------------------------------------------------------
    # Direct-out cases never reach the LLM (规则直出, §22A.5): a clear @mention,
    # or a clear strong task verb on a non-short message (a real "do work"
    # request the heuristic already routes confidently).
    clear_direct_out = bool(target_roles) or (has_task_verb and not is_short)
    in_grey_band = verdict.confidence <= _GREY_ZONE_CONFIDENCE_MAX
    eligible = (
        in_grey_band
        and bool(ambiguity_reasons)
        and not clear_direct_out
    )

    return IntentHeuristicResult(
        intent=verdict.intent,
        confidence=verdict.confidence,
        signals=tuple(signals),
        ambiguity_reasons=tuple(ambiguity_reasons),
        eligible_for_llm_fallback=eligible,
    )
