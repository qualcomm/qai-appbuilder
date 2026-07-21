# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pluggable speaker-selection strategies for multi-agent discussions.

A multi-agent discussion (docs/70-multi-agent/multi-agent-conversation-design.md §4.1) runs
an OUTER loop that decides *which* named-agent participant speaks next, while the
shared :class:`~qai.chat.application.use_cases._single_agent_turn.SingleAgentTurnKernel`
runs the INNER per-speaker tool loop. The two are orthogonal layers: the speaker
selector is to the discussion what the agentic round is to a single turn.

This module owns ONLY the OUTER concern — picking the next speaker — behind a
pluggable :class:`SpeakerSelector` protocol so the orchestrator
(:class:`~qai.chat.application.use_cases.orchestrate_discussion.OrchestrateDiscussionUseCase`)
can swap strategies per ``conversation.discussion.selector_mode`` without knowing
how the choice is made:

* :class:`RoundRobinSelector` — a deterministic, reproducible pure-function
  rotation over the named agents (the确定性兜底基准).
* :class:`ManagerAgentSelector` — asks a manager LLM to pick the next speaker,
  with a **State-Truth-First** safety net (AGENTS.md 🔴 铁律): a manager that
  times out / returns an illegal id / cannot be parsed / blows the hard round
  cap NEVER aborts the discussion — it transparently degrades to round-robin.

User pinning (``state.pinned_speaker``) is the highest priority for EVERY
selector: a pinned participant jumps the queue for exactly one turn (the
orchestrator clears the pin after that speaker runs, returning to the
underlying strategy).

Layering: this module lives in ``application/use_cases`` and depends only on the
``application.ports`` ``LLMStreamPort`` Protocol (for the manager call) + domain
types (``Participant`` / ``ParticipantId`` / ``StreamFrameType``). It imports no
adapters / apps / interfaces, so it satisfies the ``layered-chat`` /
``context-isolation`` import-linter contracts. ``SpeakerSelectionState`` is a
pure data dataclass kept here in the application layer (it carries no IO and is
not a domain invariant, so it does not belong in ``domain``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from qai.chat.application.ports import (
    LLMStreamPort,
    LLMStreamRequest,
)
from qai.chat.application.use_cases.discussion_stance_rules import RoleStance
from qai.chat.domain.content import MessageContent
from qai.chat.domain.ids import ConversationId, ParticipantId, TabId
from qai.chat.domain.mode_template import ModeTemplate
from qai.chat.domain.participant import Participant, ParticipantKind
from qai.chat.domain.stream_frame import StreamFrameType
from qai.platform.logging import get_logger

__all__ = [
    "ManagerAgentSelector",
    "RoundRobinSelector",
    "SpeakerSelectionState",
    "SpeakerSelector",
    "SpeakerTurn",
    "named_agents",
]

_log = get_logger(__name__)

# A manager turn that streams more than this many characters of selection text
# is almost certainly not a bare participant id — guard so a chatty manager
# cannot wedge the parse / blow memory. The id itself is an opaque token.
_MANAGER_REPLY_MAX_CHARS = 2_000

# Placeholder prompt for the manager's ``LLMStreamRequest``: the manager hands
# its fully-assembled instruction down via ``extra["messages"]`` (honoured by
# both LLM adapters in preference to ``prompt`` / ``history``), so ``prompt`` is
# never read. ``MessageContent`` rejects empty text, so a minimal non-empty
# sentinel is used; it never reaches the wire.
_MANAGER_PROMPT = MessageContent(
    text="(discussion manager uses extra['messages'])"
)


def named_agents(participants: Sequence[Participant]) -> list[Participant]:
    """Return only the ``NAMED_AGENT`` participants, preserving order.

    The roster handed to a selector may include the user / main-agent
    participant rows; only named agents are eligible discussion speakers
    (§4.1). A stable filter keeps round-robin rotation deterministic.
    """
    return [p for p in participants if p.kind is ParticipantKind.NAMED_AGENT]


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    """One past speaking turn recorded in the discussion history summary.

    A lightweight projection the selectors reason over (who spoke, in which
    round, and a short preview of what they said) without holding the full
    persisted :class:`~qai.chat.domain.message.Message` graph.
    """

    speaker_id: str
    display_name: str
    round_index: int
    text_preview: str = ""


@dataclass(slots=True)
class SpeakerSelectionState:
    """The mutable state a :class:`SpeakerSelector` reasons over.

    A pure data record (no IO, no domain invariants) the orchestrator owns and
    threads through ``select_next`` each round. It is intentionally an
    application-layer dataclass rather than a domain VO: it models transient
    per-discussion bookkeeping, not a persisted aggregate.

    Fields:

    * ``conversation_id`` / ``tab_id`` — identify the discussion (used to shape
      the manager's :class:`LLMStreamRequest`).
    * ``participants`` — the eligible named-agent roster, in configured order
      (round-robin rotates over this list).
    * ``history`` — append-only summary of who has spoken so far (the manager
      reads it to decide who should respond next).
    * ``round_index`` — 1-based index of the round ABOUT to be selected.
    * ``last_speaker_id`` — the participant id who spoke in the previous round
      (``None`` before the first turn); round-robin advances from here.
    * ``pinned_speaker`` — a user-pinned participant id that jumps the queue for
      exactly one turn (highest priority for every selector). The orchestrator
      clears it after the pinned speaker runs.
    * ``terminated`` — a hard stop signal (user "喊停"); any selector returns
      ``None`` immediately when set.
    """

    conversation_id: ConversationId
    tab_id: TabId
    participants: list[Participant] = field(default_factory=list)
    history: list[SpeakerTurn] = field(default_factory=list)
    round_index: int = 1
    last_speaker_id: str | None = None
    pinned_speaker: str | None = None
    terminated: bool = False
    #: The tab's currently-selected model id — the fallback model for any
    #: participant that left ``model_id`` blank ("留空则用当前标签页的模型").
    default_model_id: str | None = None
    #: The selected collaboration mode (design §26/§27), resolved once per
    #: ``execute`` from ``meta["discussion"]["selected_mode_id"]``.  ``None`` =
    #: no mode → the orchestrator keeps its existing framing / tool behaviour
    #: (deep_task zero-regression).  Carried here (not on the shared use-case
    #: instance) so concurrent discussions never cross modes.
    mode: ModeTemplate | None = None
    #: Per-role stance memory (DISC-2 P3-step1 — §22A.6 P3-a / §22A.9#1):
    #: ``participant_id → RoleStance``.  Runtime in-memory state, hydrated from
    #: the persisted ``stance_snapshot`` at ``execute`` start and re-snapshotted
    #: at the post-turn write-back boundary.  Lets each speaker continue its own
    #: previously-stated position instead of "speaking as if for the first time"
    #: every round.  Only the CURRENT speaker's own stance is injected into its
    #: system prompt (token-thrifty; never the whole table).
    stances: dict[str, RoleStance] = field(default_factory=dict)
    # ------------------------------------------------------------------
    def eligible(self) -> list[Participant]:
        """Return the eligible named-agent speakers (filtered + ordered)."""
        return named_agents(self.participants)

    def find(self, participant_id: str) -> Participant | None:
        """Return the eligible participant with this id (or ``None``)."""
        for p in self.eligible():
            if p.id.value == participant_id:
                return p
        return None

    def record(self, turn: SpeakerTurn) -> None:
        """Append a completed speaking turn to the history summary."""
        self.history.append(turn)


@runtime_checkable
class SpeakerSelector(Protocol):
    """Strategy that picks the next discussion speaker.

    ``select_next(state)`` returns the :class:`ParticipantId` of the next
    speaker, or ``None`` to END the discussion (the orchestrator then runs the
    optional judge turn / finalises). Implementations MUST be safe to call
    repeatedly and MUST NOT raise (a selection failure degrades, never aborts —
    State-Truth-First).
    """

    async def select_next(
        self, state: SpeakerSelectionState
    ) -> ParticipantId | None:
        """Return the next speaker's id, or ``None`` to end the discussion."""
        ...


def _honour_pin(state: SpeakerSelectionState) -> ParticipantId | None:
    """Return the pinned speaker (highest priority) when valid.

    The pin is the user's explicit "let X speak next" override. It beats every
    strategy. An invalid pin (id no longer in the roster) is ignored so a stale
    pin cannot wedge the discussion. Returns ``None`` when there is no
    actionable pin (the caller then runs its own strategy).
    """
    pinned = state.pinned_speaker
    if not pinned:
        return None
    target = state.find(pinned)
    if target is None:
        _log.warning(
            "chat.discussion.pin.ignored_unknown_participant",
            conversation_id=state.conversation_id.value,
            pinned_speaker=pinned,
        )
        return None
    return target.id


class RoundRobinSelector:
    """Deterministic rotation over the named agents (the确定性兜底基准).

    Pure function of ``state``: rotate to the participant AFTER
    ``state.last_speaker_id`` in the configured roster order (wrapping to the
    first when the last speaker is unknown / was the final entry), skipping
    non-named-agent rows. Returns ``None`` when there are no eligible speakers
    or the discussion was terminated. The hard round cap is the orchestrator's
    concern (``max_rounds``); this selector itself never ends a discussion on
    round count — it always offers the next speaker in rotation so the
    orchestrator's loop bound is the single source of truth.

    Reproducible + side-effect free, so it doubles as the safety-net fallback
    every other selector degrades to.
    """

    __slots__ = ()

    async def select_next(
        self, state: SpeakerSelectionState
    ) -> ParticipantId | None:
        if state.terminated:
            return None
        # User pinning is the highest priority for EVERY selector (§4.1): an
        # actionable pin jumps the queue for one turn before the rotation.
        pinned = _honour_pin(state)
        if pinned is not None:
            return pinned
        return self._rotate(state)

    @staticmethod
    def _rotate(state: SpeakerSelectionState) -> ParticipantId | None:
        roster = state.eligible()
        if not roster:
            return None
        last = state.last_speaker_id
        if last is None:
            return roster[0].id
        for idx, p in enumerate(roster):
            if p.id.value == last:
                return roster[(idx + 1) % len(roster)].id
        # The last speaker is no longer in the roster (removed mid-discussion);
        # restart the rotation from the first eligible speaker.
        return roster[0].id


class ManagerAgentSelector:
    """Ask a manager LLM to pick the next speaker, degrading to round-robin.

    The manager is given the participant roster + the discussion-so-far summary
    and asked to reply with EXACTLY one participant id (or an end sentinel). The
    selector parses that reply back to a roster member.

    State-Truth-First safety net (AGENTS.md 🔴 铁律 1/3 + §4.1 兜底铁律): the
    manager is an UNRELIABLE external resource, so a选择 that the manager
    botches must never crash the discussion. Each of the following degrades to
    :class:`RoundRobinSelector` (with a logged reason) instead of raising:

    * the manager call raises / the stream errors;
    * the manager streams nothing parseable / an unknown id;
    * the manager streams more than the hard reply-size guard;
    * the hard round cap (``max_hard_rounds``) is exceeded.

    The hard round cap is a SECOND, independent safety bound on top of the
    orchestrator's ``max_rounds``: it caps how many rounds the *manager* is
    trusted to keep choosing before we force the deterministic fallback (so a
    manager that keeps picking "continue" can never spin forever — §4.1).
    """

    __slots__ = (
        "_early_end_enabled",
        "_fallback",
        "_llm",
        "_max_hard_rounds",
        "_min_turns_before_end",
        "_model_hint",
        "_prompt_append",
    )

    #: Sentinel tokens a manager may emit to END the discussion (case-folded).
    _END_TOKENS = frozenset({"", "end", "none", "done", "stop", "finish"})

    def __init__(
        self,
        *,
        llm: LLMStreamPort,
        model_hint: str | None = None,
        max_hard_rounds: int = 24,
        fallback: SpeakerSelector | None = None,
        early_end_enabled: bool = False,
        min_turns_before_end: int = 0,
        prompt_append: str | None = None,
    ) -> None:
        self._llm = llm
        self._model_hint = model_hint
        self._max_hard_rounds = max(int(max_hard_rounds), 1)
        # The deterministic safety net every degrade path falls back to.
        self._fallback: SpeakerSelector = fallback or RoundRobinSelector()
        # DISC-2 二期 P1-step3: Manager early-END gating (§22A.3).  When OFF (the
        # default), a manager END sentinel is IGNORED (the discussion keeps going
        # via the fallback) so the manager can never end early unless the
        # ``manager_early_end_enabled`` flag is on AND the minimum-rounds floor is
        # cleared — State-Truth-First: the hard gate lives here, not in the prompt.
        self._early_end_enabled = bool(early_end_enabled)
        self._min_turns_before_end = max(int(min_turns_before_end), 0)
        # DISC-2 P4-step2 (§22A.7, final step): optional user-supplied
        # scheduling-preference text appended to the END of the moderator system
        # prompt (the immutable protocol segment always precedes it — see
        # :meth:`_manager_system_prompt`).  ``None`` / empty → no append → the
        # moderator prompt is byte-for-byte the P1-step3 prompt.  This is
        # append-only, never override: an illegal/unparseable manager reply still
        # degrades to round-robin, so the append cannot break selection.
        self._prompt_append = (prompt_append or "").strip() or None

    async def select_next(  # noqa: PLR0911 — explicit degrade-path branches
        self, state: SpeakerSelectionState
    ) -> ParticipantId | None:
        if state.terminated:
            return None

        # Pinning is the highest priority for EVERY selector (incl. manager):
        # honour an actionable user pin before consulting the manager at all.
        pinned = _honour_pin(state)
        if pinned is not None:
            return pinned

        roster = state.eligible()
        if not roster:
            return None

        # Hard round cap — independent of the orchestrator's max_rounds. Past
        # it, stop trusting the manager and use deterministic rotation so a
        # never-ending manager cannot run away (State-Truth-First).
        if state.round_index > self._max_hard_rounds:
            _log.warning(
                "chat.discussion.manager.degraded_round_cap",
                conversation_id=state.conversation_id.value,
                round_index=state.round_index,
                max_hard_rounds=self._max_hard_rounds,
            )
            return await self._fallback.select_next(state)

        try:
            reply = await self._ask_manager(state, roster)
        except Exception as exc:  # noqa: BLE001 — never abort the discussion
            _log.warning(
                "chat.discussion.manager.degraded_exception",
                conversation_id=state.conversation_id.value,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return await self._fallback.select_next(state)

        choice = self._parse_choice(reply, roster)
        if choice is _PARSE_END:
            # Manager early-END gate (§22A.3, State-Truth-First — do NOT trust the
            # LLM to self-restrain).  The manager only ENDS the discussion when the
            # ``manager_early_end_enabled`` flag is on AND we are past the shared
            # minimum-rounds floor; otherwise the END is IGNORED and we degrade to
            # the deterministic fallback so the discussion keeps going.  This also
            # fixes the pre-existing defect where a manager END ended the
            # discussion unconditionally regardless of any flag.
            if (
                self._early_end_enabled
                and state.round_index > self._min_turns_before_end
            ):
                return None
            _log.info(
                "chat.discussion.manager.end_ignored",
                conversation_id=state.conversation_id.value,
                round_index=state.round_index,
                early_end_enabled=self._early_end_enabled,
                min_turns_before_end=self._min_turns_before_end,
            )
            return await self._fallback.select_next(state)
        if choice is None:
            _log.warning(
                "chat.discussion.manager.degraded_unparseable",
                conversation_id=state.conversation_id.value,
                reply_preview=reply[:120],
            )
            return await self._fallback.select_next(state)
        return choice

    async def _ask_manager(
        self,
        state: SpeakerSelectionState,
        roster: list[Participant],
    ) -> str:
        """Run one manager LLM turn and return its raw text reply.

        Hands a fully-assembled instruction down via ``extra["messages"]`` (the
        same contract the sub-agent / orchestrator use), so no system-prompt
        plumbing is needed. Drains chunk frames into text, stops on the first
        ERROR / END, and caps the accumulated size.
        """
        wire = [
            {"role": "system", "content": self._manager_system_prompt(roster)},
            {"role": "user", "content": self._manager_user_prompt(state)},
        ]
        request = LLMStreamRequest(
            conversation_id=state.conversation_id,
            tab_id=state.tab_id,
            prompt=_MANAGER_PROMPT,
            history=(),
            model_hint=self._model_hint,
            extra={"messages": wire},
        )
        parts: list[str] = []
        size = 0
        async for frame in self._llm.stream(request):
            if frame.frame_type is StreamFrameType.CHUNK:
                text = frame.payload.get("text", "")
                if isinstance(text, str) and text:
                    parts.append(text)
                    size += len(text)
                    if size >= _MANAGER_REPLY_MAX_CHARS:
                        break
            elif frame.frame_type is StreamFrameType.ERROR:
                # Surface as an exception so ``select_next`` degrades cleanly.
                raise _ManagerStreamError(
                    frame.payload.get("message", "manager stream error")
                )
            elif frame.frame_type is StreamFrameType.END:
                break
        return "".join(parts)

    def _manager_system_prompt(self, roster: list[Participant]) -> str:
        """Assemble the moderator system prompt.

        Ordering is load-bearing (DISC-2 P4-step2, §22A.7): the IMMUTABLE
        protocol segment is built FIRST and verbatim — the MODERATOR role
        definition, the "reply with EXACTLY one participant id" instruction, the
        END sentinel block (only when early-end is enabled), and the roster list.
        Any user-supplied scheduling preference (``self._prompt_append``) is
        appended LAST so it can never override or shadow the protocol that
        precedes it; it is framed as *advisory* so the LLM treats it as a
        preference, not a protocol change.  Append-only, never override: an
        illegal manager reply still degrades to round-robin (``select_next``).
        """
        lines = [
            "You are the MODERATOR of a multi-agent discussion. Your only job "
            "is to choose which participant speaks next.",
        ]
        if self._early_end_enabled:
            # END is allowed — but guard against premature convergence (§22A.3).
            lines.append(
                "Reply with EXACTLY one participant id from the roster below "
                "and nothing else. To end the discussion, reply with: END."
            )
            lines.append(
                "Only reply END when the core question has been addressed by "
                "multiple roles with no major angle left uncovered; do NOT end "
                f"before round {self._min_turns_before_end + 1}."
            )
        else:
            # END is NOT offered to the manager when the early-end flag is off —
            # keep the prompt consistent with the hard gate in ``select_next``
            # (which ignores any END the manager emits anyway).
            lines.append(
                "Reply with EXACTLY one participant id from the roster below "
                "and nothing else."
            )
        lines.append("")
        lines.append("Roster (id — name):")
        for p in roster:
            name = p.display_name or p.id.value
            lines.append(f"- {p.id.value} — {name}")
        # DISC-2 P4-step2 (§22A.7): user scheduling preference appended LAST, so
        # the immutable protocol segment above is never overridden.  Advisory
        # framing keeps the manager bound to "reply with exactly one id".
        if self._prompt_append:
            lines.append("")
            lines.append(
                "Scheduling preference (advisory, does not change the rules "
                "above): " + self._prompt_append
            )
        return "\n".join(lines)

    def _manager_user_prompt(self, state: SpeakerSelectionState) -> str:
        if not state.history:
            return (
                "The discussion is just starting. Choose the first speaker "
                "(reply with their id only)."
            )
        recent = state.history[-12:]
        transcript = "\n".join(
            f"[{t.display_name or t.speaker_id}]: {t.text_preview}"
            for t in recent
        )
        if self._early_end_enabled:
            closing = "Who should speak next? Reply with their id only, or END to conclude."
        else:
            closing = "Who should speak next? Reply with their id only."
        return (
            "Discussion so far:\n"
            f"{transcript}\n\n"
            f"{closing}"
        )

    def _parse_choice(
        self,
        reply: str,
        roster: list[Participant],
    ) -> ParticipantId | None | object:
        """Map a raw manager reply to a roster id / end sentinel / unparseable.

        Returns:

        * :data:`_PARSE_END` — the manager asked to end the discussion;
        * a :class:`ParticipantId` — a recognised roster member;
        * ``None`` — could not be parsed (caller degrades to round-robin).
        """
        token = reply.strip()
        folded = token.casefold()
        if folded in self._END_TOKENS:
            return _PARSE_END
        # Exact id match first (the requested format).
        for p in roster:
            if p.id.value == token:
                return p.id
        # Tolerant fallbacks: the manager may wrap the id in quotes / prose or
        # answer with the display name. Look for any roster id / name as a
        # whitespace- or punctuation-delimited token in the reply.
        for p in roster:
            if _contains_token(folded, p.id.value.casefold()):
                return p.id
        for p in roster:
            name = (p.display_name or "").casefold()
            if name and _contains_token(folded, name):
                return p.id
        return None


class _ManagerStreamError(RuntimeError):
    """Internal: the manager LLM stream surfaced an ERROR frame."""


#: Sentinel returned by ``_parse_choice`` when the manager asked to end the
#: discussion (distinct from ``None`` = unparseable → degrade to round-robin).
_PARSE_END: object = object()


def _contains_token(haystack: str, needle: str) -> bool:
    """Return True iff ``needle`` appears in ``haystack`` as a delimited token.

    Avoids matching a short id as a substring of an unrelated word; both inputs
    are already case-folded.
    """
    if not needle:
        return False
    start = 0
    nlen = len(needle)
    while True:
        idx = haystack.find(needle, start)
        if idx < 0:
            return False
        before_ok = idx == 0 or not haystack[idx - 1].isalnum()
        after = idx + nlen
        after_ok = after >= len(haystack) or not haystack[after].isalnum()
        if before_ok and after_ok:
            return True
        start = idx + 1
