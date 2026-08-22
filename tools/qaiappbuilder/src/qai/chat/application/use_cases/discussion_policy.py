# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Discussion Policy Planner (§21.4) — intent × state → execution policy.

Layer 2 of the four-layer discussion orchestration (§21.2).  A **pure function**
that maps an :class:`~qai.chat.application.use_cases.discussion_intent.IntentResult`
plus the current ``discussion_state`` and a small, fully-resolved context
(roster ids, last-active speaker, recent non-judge speaker, mentioned ids) onto a
:class:`DiscussionPolicy`: which execution path runs, how many rounds, which
participants speak, which interaction-framing mode they wear, whether the judge
turn runs, and what state to write back afterwards.

Keeping the planner pure (every input passed in, no IO) means the orchestrator
resolves the live context once (history scan) and the routing decision itself is
trivially unit-testable (§21.15 Step 2).  The routing table is §21.4 verbatim.

Critical rules baked in here (so the orchestrator stays a thin dispatcher):

* **§21.14#3 directed strict fallback** — a directed intent whose @mentions did
  NOT resolve must fall back to a SINGLE responder (last-active → recent
  non-judge → first), NEVER to the full roster (that is the over-discussion this
  feature exists to kill).  The planner emits a single-element ``participants``.
* **§21.14#5 idle + social** — the opening "Hi" (no history) resolves the
  responder to ``roster[0]`` via the §21.6 priority ladder, and stays
  lightweight (never full).
* **§21.11 grey-zone never escalates to full** — deep_task is the ONLY path that
  yields ``executionPath == "full"``; every other intent is scoped/lightweight.

Layering: ``application/use_cases`` — pure logic over the sibling intent module +
stdlib.  No ports / domain / adapters, so the layering contracts hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from qai.chat.application.use_cases.discussion_intent import (
    DiscussionState,
    IntentResult,
)

__all__ = [
    "ExecutionPath",
    "FramingMode",
    "DiscussionPolicy",
    "ResponderContext",
    "plan_policy",
    "select_social_responder",
    "score_recent_contributor",
]


ExecutionPath = Literal["lightweight", "scoped", "full"]
FramingMode = Literal["social_mode", "followup_mode", "debate_mode", "wrapup_mode"]


@dataclass(frozen=True, slots=True)
class ResponderContext:
    """The fully-resolved, live discussion context the planner reasons over.

    The orchestrator computes this ONCE per turn (a single history scan) and
    hands it to :func:`plan_policy`, keeping the planner pure.  All ids are
    participant ids (``Participant.id.value``).

    * ``roster_ids`` — eligible named-agent ids, in configured order.
    * ``mentioned_ids`` — @mention-resolved ids, in mention order (may be empty
      even when the intent is directed — a typo that resolved nothing; the
      planner then applies the strict single-responder fallback, §21.14#3).
    * ``last_active_speaker`` — the most recent SUBSTANTIVE speaker id (Path A
      replies never set this — §21.6); ``None`` before any real turn.
    * ``recent_non_judge_speaker`` — the most recent non-judge participant who
      spoke (history scan); ``None`` when none.
    * ``judge_id`` — the configured/derived judge id (so a scoped responder
      ladder can avoid handing a follow-up to the judge when alternatives
      exist); informational, may be ``None``.
    * ``enable_judge`` — whether the conversation has the judge turn enabled
      (``discussion.enable_judge``).  The deep_task ``judge`` decision mirrors
      THIS ("现状" in the §21.4 routing table) instead of being hard-coded, so
      a deep_task with ``enable_judge=False`` reports ``judge=False`` faithfully
      (§21.14#1 zero-change: judge runs exactly when the existing config says).
    * ``configured_max_rounds`` — the conversation's configured full-discussion
      round cap (``discussion.max_rounds``).  Used to scale a directed_deep_task
      to ``min(config, len(mentioned)×2)`` per the §21.4 deep_task+@mention row.

    DISC-2 二期 convergence-control flags (tail-appended; DISC-2 P1-step1).
    Read out by the orchestrator from ``meta["discussion"]`` (missing key = OFF)
    and carried into the planner context for downstream steps.  P1-step1 only
    READS them out + logs — the planner does NOT consume them yet (``can_early_stop``
    stays hard-coded ``False``; ConvergenceController consumption lands P1-step2+):

    * ``convergence_control_enabled`` — master switch for convergence control.
    * ``manager_early_end_enabled`` — allow the manager selector to END early.
    * ``soft_stop_enabled`` — allow soft-stop convergence.
    * ``soft_stop_mode`` — soft-stop aggressiveness (default ``"conservative"``).

    DISC-2 P3-step2 (§22A.6 P3-c) recent-contribution scoring inputs
    (tail-appended).  Pre-aggregated by the orchestrator (a single history
    scan) so the planner stays a pure function — it never reads the message
    graph itself.  Both default empty so the scoring term is INERT on the first
    turn / when no history exists → ``select_social_responder`` then behaves
    byte-for-byte as before (§21.6 ladder unchanged):

    * ``recent_contributions`` — one ``(participant_id, recency_rank,
      content_length)`` tuple per recent contributor.  ``recency_rank`` is
      0-based newest-first (0 = most recent speaker); ``content_length`` is an
      approximate character count of that contributor's most recent turn (the
      orchestrator may use ``text_preview`` length as a proxy).  Empty → the
      scoring candidate is skipped entirely.
    * ``user_message_terms`` — deterministic, normalised terms parsed from the
      latest user message (for the optional mention/topic-overlap score
      component).  Empty → that component contributes 0.
    """

    roster_ids: tuple[str, ...] = ()
    mentioned_ids: tuple[str, ...] = ()
    last_active_speaker: str | None = None
    recent_non_judge_speaker: str | None = None
    judge_id: str | None = None
    enable_judge: bool = False
    configured_max_rounds: int = 0
    convergence_control_enabled: bool = False
    manager_early_end_enabled: bool = False
    soft_stop_enabled: bool = False
    soft_stop_mode: str = "conservative"
    recent_contributions: tuple[tuple[str, int, int], ...] = ()
    user_message_terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscussionPolicy:
    """The execution policy for ONE user turn (§21.4 output contract).

    * ``execution_path`` — ``lightweight`` (Path A) / ``scoped`` (Path B) /
      ``full`` (Path C = current behaviour, byte-for-byte).
    * ``max_rounds`` — the OUTER speaker-round budget for this turn.  For
      lightweight it is ``1``; scoped ``1..2``; full is the caller's configured
      cap (the planner emits ``0`` as "use the configured full cap" sentinel so
      it never hard-codes the deep_task budget — §21.14#1 zero-change).
    * ``participants`` — the concrete participant ids that speak (already
      resolved + ordered).  Empty only for ``full`` (the selector picks live).
    * ``framing_mode`` — the interaction mode each speaker wears this turn.
    * ``judge`` — whether the final judge turn runs (only ``full`` — §21.5 H-3).
    * ``can_early_stop`` — reserved; MVP always ``False`` (soft-stop is phase 2).
    * ``update_state_to`` — the ``discussion_state`` to persist after the turn;
      ``None`` means "leave unchanged" (social/ack greetings — §21.6).
    * ``force_no_tools`` — Path A hard tool ban (core-enforced, not framing —
      §21.14#2): the lightweight responder gets an empty tool set + 1 inner
      round so a chatty model cannot run a tool on a "Hi".
    """

    execution_path: ExecutionPath
    framing_mode: FramingMode
    max_rounds: int
    judge: bool
    can_early_stop: bool
    update_state_to: DiscussionState | None
    force_no_tools: bool
    participants: tuple[str, ...] = field(default_factory=tuple)


#: Sentinel for ``max_rounds`` meaning "use the caller's configured full cap"
#: (the planner must NOT hard-code the deep_task budget — keeps Path C
#: byte-for-byte unchanged, §21.14#1).
USE_CONFIGURED_FULL_ROUNDS = 0


# ---------------------------------------------------------------------------
# DISC-2 P3-step2 (§22A.6 P3-c) — recent-contribution scoring candidate.
# ---------------------------------------------------------------------------
#: Recency weight: every step further back in the recency ranking subtracts
#: this much from a candidate's score (so a more-recent contributor edges out an
#: older one, all else equal).
_RECENCY_WEIGHT = 2.0
#: Content-length weight per bucket.  We bucket the (approximate) character
#: count into coarse tiers (see ``_length_bucket``) rather than using the raw
#: length, so a single very long turn cannot dominate the ladder (conservative —
#: §22A.6 P3-c "不要被超长发言一面倒").
_CONTENT_LENGTH_WEIGHT = 1.0
#: Per-overlapping-term weight for the latest user message ↔ candidate-id
#: overlap.  Capped by ``_MAX_MENTION_OVERLAP`` so a keyword-stuffed message
#: cannot swamp the recency/length signal.
_MENTION_OVERLAP_WEIGHT = 1.5
_MAX_MENTION_OVERLAP = 2
#: Coarse content-length buckets (approx chars → bucket score).  Conservative:
#: caps at 3 so the longest turns gain only a modest edge.
_LENGTH_BUCKETS: tuple[tuple[int, int], ...] = (
    (240, 3),
    (120, 2),
    (40, 1),
)


def _length_bucket(content_length: int) -> int:
    """Bucket an approximate character count into a small, capped tier (0..3).

    Bucketing (rather than the raw length) is deliberate: it keeps the
    content-length signal a gentle tie-breaker so one extremely long turn never
    dominates the responder ladder (§22A.6 P3-c — conservative).
    """
    for threshold, bucket in _LENGTH_BUCKETS:
        if content_length >= threshold:
            return bucket
    return 0


def score_recent_contributor(ctx: ResponderContext) -> str | None:
    """Pick the best recent contributor as a social/lightweight responder.

    DISC-2 P3-step2 (§22A.6 P3-c).  A pure, deterministic scoring candidate that
    runs ONLY as one rung of :func:`select_social_responder` (after the @mention
    and ``last_active_speaker`` rungs, before the recent-non-judge / first
    rungs).  Returns ``None`` — and is therefore completely inert — whenever
    there is no ``recent_contributions`` material (first turn / no history),
    which is exactly why the legacy ladder behaviour is preserved byte-for-byte
    when the orchestrator supplies no scoring inputs.

    The score per candidate (§22A.6 P3-c formula) is::

        score = recency + content_length + mention_overlap

    where recency rewards more-recent speakers, content_length is a coarse,
    capped bucket of the turn's length (so a single very long turn cannot
    dominate — conservative), and mention_overlap rewards a candidate whose id
    appears among the latest user message's terms (0 when no material).  The
    ``non_judge`` term of the §22A.6 P3-c formula is realised as a HARD
    exclusion (a judge is never a valid social responder — State-Truth-First:
    the core layer filters it out rather than relying on a soft bonus that a
    long/recent judge turn could outscore).  Only roster members participate;
    ties resolve to the more recent (lower ``recency_rank``) then to roster
    order (deterministic).
    """
    if not ctx.recent_contributions or not ctx.roster_ids:
        return None

    roster = set(ctx.roster_ids)
    terms = {t for t in ctx.user_message_terms if t}
    best_id: str | None = None
    best_score: float | None = None
    best_rank: int | None = None

    for pid, recency_rank, content_length in ctx.recent_contributions:
        # Hard-exclude the judge + any non-roster (stale) candidate — a judge is
        # never selected as the social responder (§22A.6 P3-c).
        if pid not in roster or pid == ctx.judge_id:
            continue
        recency = -_RECENCY_WEIGHT * float(max(recency_rank, 0))
        length_score = _CONTENT_LENGTH_WEIGHT * float(_length_bucket(content_length))
        overlap = 0
        if terms:
            pid_low = pid.casefold()
            overlap = sum(
                1 for term in terms if term and (term in pid_low or pid_low in term)
            )
            overlap = min(overlap, _MAX_MENTION_OVERLAP)
        mention_score = _MENTION_OVERLAP_WEIGHT * float(overlap)
        score = recency + length_score + mention_score

        # Strictly-greater keeps the FIRST (more-recent, since recent_contributions
        # is newest-first) candidate on a tie — deterministic.
        if (
            best_score is None
            or score > best_score
            or (
                score == best_score
                and best_rank is not None
                and recency_rank < best_rank
            )
        ):
            best_id = pid
            best_score = score
            best_rank = recency_rank

    return best_id


def select_social_responder(ctx: ResponderContext) -> str | None:
    """Resolve the single responder for a social/ack/directed-fallback turn.

    Priority (§21.6 + §22A.6 P3-c):
    ① an @mentioned role → ② ``last_active_speaker`` → ②.5 the highest-scoring
    recent contributor (:func:`score_recent_contributor`; INERT when there is no
    history) → ③ the most recent non-judge participant → ④ the first roster
    member.  Returns ``None`` only when the roster is empty (the caller then
    emits no responder).

    The §22A.6 P3-c scoring rung sits AFTER ``last_active_speaker`` (so a known
    last-active speaker still wins, preserving every existing test that supplies
    one) and only fires when the orchestrator pre-aggregated
    ``recent_contributions``.  Without that material — the common case for the
    existing responder tests and the opening "Hi" — the rung returns ``None`` and
    the legacy ③④ ladder runs verbatim.

    Used for both the lightweight social responder AND the directed strict
    fallback (§21.14#3): a directed intent whose mentions did not resolve uses
    this same single-responder ladder, NEVER the full roster.
    """
    if ctx.mentioned_ids:
        return ctx.mentioned_ids[0]
    if ctx.last_active_speaker and ctx.last_active_speaker in ctx.roster_ids:
        return ctx.last_active_speaker
    scored = score_recent_contributor(ctx)
    if scored is not None:
        return scored
    if (
        ctx.recent_non_judge_speaker
        and ctx.recent_non_judge_speaker in ctx.roster_ids
    ):
        return ctx.recent_non_judge_speaker
    return ctx.roster_ids[0] if ctx.roster_ids else None


def _scoped_participants(ctx: ResponderContext) -> tuple[str, ...]:
    """Resolve the participant subset for a (non-directed) scoped follow-up.

    Prefer a small, relevant subset: the last-active speaker plus the most
    recent non-judge speaker (deduped, roster-filtered).  Falls back to the
    single-responder ladder when no prior speaker is known (§21.5: a scoped
    turn that cannot find relevant roles uses last_active → recent → first,
    and NEVER escalates to the full roster).
    """
    ordered: list[str] = []
    for cand in (ctx.last_active_speaker, ctx.recent_non_judge_speaker):
        if cand and cand in ctx.roster_ids and cand not in ordered:
            ordered.append(cand)
    if ordered:
        return tuple(ordered)
    one = select_social_responder(ctx)
    return (one,) if one else ()


def plan_policy(
    intent: IntentResult,
    *,
    state: DiscussionState,
    ctx: ResponderContext,
) -> DiscussionPolicy:
    """Map ``intent`` × ``state`` × ``ctx`` onto a :class:`DiscussionPolicy`.

    Implements the §21.4 routing table verbatim, with the §21.14 clarifications
    (#3 directed strict fallback, #5 idle+social responder, #1 full uses the
    configured cap, #2 lightweight force-no-tools).  Pure + deterministic.
    """
    subtype = intent.subtype

    # ── Path A — Lightweight (social / ack) ──────────────────────────────────
    # social_greeting / ack_passive: a single brief reply, no state change.
    # thanks_or_closing: a single brief reply + transition to ``closed``.
    if intent.intent == "social" or (
        intent.intent == "ack" and subtype == "ack_passive"
    ):
        responder = select_social_responder(ctx)
        participants = (responder,) if responder else ()
        if subtype == "thanks_or_closing":
            return DiscussionPolicy(
                execution_path="lightweight",
                framing_mode="wrapup_mode",
                max_rounds=1,
                judge=False,
                can_early_stop=False,
                update_state_to="closed",
                force_no_tools=True,
                participants=participants,
            )
        # social_greeting / ack_passive → social_mode, state UNCHANGED (§21.6:
        # social is not a persisted state, so "Hi Hi Hi" never thrashes it).
        return DiscussionPolicy(
            execution_path="lightweight",
            framing_mode="social_mode",
            max_rounds=1,
            judge=False,
            can_early_stop=False,
            update_state_to=None,
            force_no_tools=True,
            participants=participants,
        )

    # ── Path C — Full (deep_task) ────────────────────────────────────────────
    # The ONLY path that runs the full selector loop + optional judge.  Behaviour
    # is byte-for-byte the current orchestrator (§21.14#1): empty participants
    # (the selector picks live), configured full cap, debate_mode (= the existing
    # framing branch), judge per CONFIG ("现状" — mirrors ``ctx.enable_judge``,
    # NOT hard-coded).  Directed deep_task (@role + task verb) scopes participants
    # to the mentioned roles, still runs as a full turn, and CAPS rounds to
    # ``min(config, len(mentioned)×2)`` (§21.4 deep_task+@mention row).  DISC-1
    # §22.5 step1: ``subtype == "implement"`` (an @mention + a窄 implement verb)
    # is routed through this SAME directed branch byte-for-byte — implement is
    # currently a discussion like ``directed_deep_task`` (NO tool放开 here); the
    # real implementation-mode編排 lands in step3, not in the policy planner.
    if intent.intent == "deep_task":
        if subtype in ("directed_deep_task", "implement") and intent.target_roles:
            participants = tuple(
                pid for pid in ctx.mentioned_ids if pid in ctx.roster_ids
            )
            # Strict: a directed deep_task with no resolvable mention degrades to
            # the single-responder ladder (NEVER the full roster — §21.14#3).
            if not participants:
                one = select_social_responder(ctx)
                participants = (one,) if one else ()
            # Scale the round budget to the addressed subset: min(config,
            # len(mentioned)×2).  Falls back to the configured cap when the
            # config is unknown (0 sentinel) so we never under-run the floor.
            config_cap = (
                ctx.configured_max_rounds if ctx.configured_max_rounds >= 1 else None
            )
            scaled = max(len(participants), 1) * 2
            directed_rounds = (
                min(config_cap, scaled) if config_cap is not None else scaled
            )
            return DiscussionPolicy(
                execution_path="full",
                framing_mode="debate_mode",
                max_rounds=directed_rounds,
                judge=ctx.enable_judge,
                can_early_stop=False,
                update_state_to="awaiting_user",
                force_no_tools=False,
                participants=participants,
            )
        return DiscussionPolicy(
            execution_path="full",
            framing_mode="debate_mode",
            max_rounds=USE_CONFIGURED_FULL_ROUNDS,
            judge=ctx.enable_judge,
            can_early_stop=False,
            update_state_to="awaiting_user",
            force_no_tools=False,
            participants=(),  # full selector picks speakers live
        )

    # ── Path B — Scoped (follow_up / directed_follow_up / continue_request) ───
    # directed_follow_up: STRICT — only the @mentioned roles speak; an
    # unresolved mention falls back to a SINGLE responder, never the full roster.
    if intent.intent == "directed_follow_up":
        participants = tuple(
            pid for pid in ctx.mentioned_ids if pid in ctx.roster_ids
        )
        if not participants:
            one = select_social_responder(ctx)
            participants = (one,) if one else ()
        return DiscussionPolicy(
            execution_path="scoped",
            framing_mode="followup_mode",
            max_rounds=1,
            judge=False,
            can_early_stop=False,
            update_state_to="active_discussion",
            force_no_tools=False,
            participants=participants,
        )

    # continue_request: continue the discussion with the relevant subset.
    if intent.intent == "ack" and subtype == "continue_request":
        return DiscussionPolicy(
            execution_path="scoped",
            framing_mode="followup_mode",
            max_rounds=2,
            judge=False,
            can_early_stop=False,
            update_state_to="active_discussion",
            force_no_tools=False,
            participants=_scoped_participants(ctx),
        )

    # follow_up (and any residual ack that is not passive/continue): scoped
    # subset, 1..2 rounds, no judge.
    if intent.intent == "follow_up":
        return DiscussionPolicy(
            execution_path="scoped",
            framing_mode="followup_mode",
            max_rounds=2,
            judge=False,
            can_early_stop=False,
            update_state_to="active_discussion",
            force_no_tools=False,
            participants=_scoped_participants(ctx),
        )

    # Defensive default (should be unreachable — every public intent is handled
    # above).  Degrade to a single scoped follow-up, never full (§21.11).
    return DiscussionPolicy(
        execution_path="scoped",
        framing_mode="followup_mode",
        max_rounds=1,
        judge=False,
        can_early_stop=False,
        update_state_to="active_discussion",
        force_no_tools=False,
        participants=_scoped_participants(ctx),
    )
