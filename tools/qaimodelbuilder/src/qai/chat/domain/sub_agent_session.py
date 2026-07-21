# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``SubAgentSession`` aggregate for the chat bounded context.

A :class:`SubAgentSession` is the "session / memory" of a sub-agent that
was spawned from a parent conversation.  It carries the sub-agent's own
persisted context -- its STRUCTURED :class:`~qai.chat.domain.message.Message`
transcript (:attr:`SubAgentSession.messages`) -- so that:

* the main agent can **wake the sub-agent up** and resume its work (the
  resume path rebuilds the feed-the-model wire from ``messages`` via
  ``rebuild_history_wire_messages``, the same口径 the main agent uses);
* the user can **take it over** (``take_over_by_user``) and continue the
  thread manually.

Lifecycle
---------
A sub-agent session is born ``RUNNING`` (default), then settles into a
terminal state -- ``DONE`` (finished normally), ``ERROR`` (failed),
``INTERRUPTED`` (aborted) -- or transitions to ``USER_OWNED`` once the
user takes it over.  It follows the lifecycle of its parent conversation:
when the parent conversation is deleted, its sub-agent sessions go with it.

Mutability model
----------------
Unlike the frozen value objects, this aggregate is *mutable* -- it is one
of the places where state changes happen (``messages`` grows, ``status`` /
``owner`` flip, ``rounds`` increments).  All mutators here bump
``updated_at`` and emit no platform side-effects (no logging / no IO),
mirroring :class:`~qai.chat.domain.conversation.Conversation`.

The :attr:`messages` transcript is the AUTHORITATIVE display + replay source
(SUBAGENT-UNIFY-6): the detail route serialises it directly and the
feed-the-model wire is rebuilt from it on demand. The legacy flat OpenAI
``wire_messages`` column was removed (migration 048) -- a sub-agent no longer
persists an opaque wire snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from qai.chat.domain.ids import ConversationId, MessageId, SubAgentSessionId
from qai.chat.domain.message import Message
from qai.platform.time import ensure_aware_utc

_MAX_PROMPT_PREVIEW_LENGTH: int = 500


class SubAgentSessionStatus(str, Enum):
    """Lifecycle status of a :class:`SubAgentSession`."""

    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    USER_OWNED = "user_owned"


class SubAgentOwner(str, Enum):
    """Who currently drives a :class:`SubAgentSession`.

    ``MAIN_AGENT`` while the orchestrating agent owns the thread;
    ``USER`` once a human has taken it over.
    """

    MAIN_AGENT = "main_agent"
    USER = "user"


# Statuses from which no further *automatic* (main-agent driven) transition
# is allowed.  A user take-over is permitted from any non-terminal state and
# is handled explicitly in :meth:`SubAgentSession.take_over_by_user`.
_TERMINAL_STATUSES: frozenset[SubAgentSessionStatus] = frozenset(
    {
        SubAgentSessionStatus.DONE,
        SubAgentSessionStatus.ERROR,
        SubAgentSessionStatus.INTERRUPTED,
    },
)


@dataclass(slots=True, kw_only=True)
class SubAgentSession:
    """The aggregate root for a sub-agent's persisted session.

    Mutating methods keep ``updated_at`` consistent and apply light
    state-machine checks (a terminal session cannot silently re-transition
    to a different terminal state), mirroring the
    :class:`~qai.chat.domain.tab.ConversationTab` style.
    """

    id: SubAgentSessionId
    # ROOT conversation id: the "top-of-the-tree" main-agent conversation this
    # sub-agent belongs to, regardless of how deep it is. A first-level sub-agent
    # AND every grand / great-grand / … sub-agent under it all carry the SAME
    # ``root_conversation_id`` (the main chat tab's conversation), because the
    # persisted transcript / broadcaster fan-out / cascade-delete lifecycle all
    # follow that single root conversation. This is the honest name for the
    # column that migration 030 mis-named ``parent_conversation_id`` — see
    # migration 049 for the rename. The direct parent (the sub-agent immediately
    # above this one, if any) is carried by :attr:`parent_subagent_id`; the main
    # agent is depth 0 and lives OUTSIDE this table.
    root_conversation_id: ConversationId
    # Direct-parent sub-agent id. ``None`` = my direct parent is the main agent
    # (I am a depth-1 sub-agent under ``root_conversation_id``); non-``None`` =
    # my direct parent is another sub-agent row (a grand / great-grand / … cell
    # in the tree). Persisted with a soft reference (plain TEXT, no FK) so a
    # deletion of the parent sub-agent row cascades via the shared
    # ``root_conversation_id`` cascade instead of a self-FK — sub-agent rows
    # under one root conversation are always deleted together. Tail-appended
    # (§3.1, migration 049); ``None`` on legacy rows is the correct default
    # (they were all depth-1 in the old grand-sub-agent branch too).
    parent_subagent_id: SubAgentSessionId | None = None
    # Recursion depth of this sub-agent (1 = first-level under the main agent,
    # 2 = grand sub-agent, 3 = great-grand, …). The main agent conceptually is
    # depth 0 but is NOT persisted here. Tail-appended (§3.1, migration 049) so
    # existing rows default to 1 — which is truthful for every row that was
    # written before this migration (the pre-α ``_spawn_grand_sub_agent`` grand
    # branch was locked to ``allow_spawn=False`` and could only produce
    # depth-1 rows structurally, since the "grand" branch never persisted its
    # own tree relationship — it only appeared inside the tool result string).
    depth: int = 1
    parent_message_id: MessageId | None = None
    # Which agent PROFILE backs this session — the sub-agent's
    # :class:`~qai.chat.domain.agent_profile.AgentProfile` name (e.g.
    # ``"general"`` / ``"explore"``). Persisted at spawn so a RESUME keeps the
    # profile the sub-agent was created with (an explore sub-agent stays
    # read-only on follow-up turns). The historical default ``"agent"`` is
    # treated as ``general`` by ``resolve_profile`` for backward compatibility
    # with already-stored rows.
    subagent_type: str = "agent"
    title: str = ""
    # Truncated (<= 500 chars) preview of the initial task description, for
    # display in fold blocks / wake-up pickers without loading wire history.
    prompt_preview: str = ""
    status: SubAgentSessionStatus = SubAgentSessionStatus.RUNNING
    owner: SubAgentOwner = SubAgentOwner.MAIN_AGENT
    rounds: int = 0
    created_at: datetime
    updated_at: datetime
    # Optimistic-lock version (block 4). Bumped by the repository on each
    # successful save; used for compare-and-swap so two concurrent writers
    # (main-agent resume + user take-over of the same id) cannot silently
    # clobber each other's whole-row UPSERT. Tail-appended (§3.1); defaults to
    # 0 so existing callers / rows are unaffected.
    version: int = 0
    # Cumulative token usage summed across every round of this run (parity
    # with the main agent, which accumulates per-round usage and surfaces the
    # running total on its terminal END frame). Tail-appended (§3.1); ``None``
    # = no usage seen (existing rows / light callers / unit stubs unaffected).
    # Shape is the provider's usage dict, e.g.
    # ``{"prompt_tokens": N, "completion_tokens": M, "total_tokens": K}``.
    usage: dict[str, Any] | None = None
    # DEPRECATED (no longer the primary 回看 path): per-round prompt snapshots
    # (round number → that round's sent OpenAI wire). Originally fed a sub-agent-
    # specific "查看提示词快照" top-of-tab affordance. That sub-agent-specific UI
    # was REMOVED — the sub-agent now saves each round's snapshot into the SHARED
    # ``PromptSnapshotStorePort`` (the SAME store the main agent uses) and stamps
    # the per-round ``request_id`` onto its persisted assistant turn, so a
    # standalone tab reuses the main agent's STANDARD per-message 📄 button. The
    # field + ``record_round_snapshot`` mutator + migration-035 column are KEPT
    # for backward compatibility with already-persisted rows (read-only round-
    # trip); new runs leave this ``None``. Do NOT build new features on it.
    round_snapshots: dict[int, list[dict[str, Any]]] | None = None
    # Replace-last semantics — the most recent round's
    # ``last_round_prompt_tokens`` (best approximation of CURRENT context
    # occupancy), mirroring ``CodingSession.last_input_tokens``. This is NOT a
    # cumulative sum (that is :attr:`usage`); it tracks "how big is the wire we
    # would send NEXT" for the standalone tab's ctx-badge. Tail-appended
    # (§3.1); ``None`` = no positive last-round figure seen yet (existing rows /
    # light callers / unit stubs unaffected).
    last_prompt_tokens: int | None = None
    # Whether this sub-agent was GRANTED the ability to spawn its own
    # sub-agents at the moment it was spawned (i.e. the spawning main agent's
    # per-tab "allow first-level sub-agents to spawn their own sub-agents"
    # switch — ``allow_child_spawn`` — was ON). Persisted at spawn (tail-
    # appended §3.1, migration 045) so that when the USER later takes the
    # sub-agent over in a standalone tab, the front-end can DEFAULT its "allow
    # THIS sub-agent to create sub-agents" toggle to ON (the user may still
    # turn it off — the grant is a default, not a lock). Default ``False`` =
    # not granted (existing rows / light callers / unit stubs unaffected —
    # the historical hard recursion guard).
    allow_spawn: bool = False
    # The model this sub-agent runs with — the SINGLE source of truth for the
    # context-budget denominator (State-Truth-First 铁律 1 / 铁律 4). A sub-agent
    # defaults to its PARENT's model at spawn, but the user may switch THIS
    # sub-agent's model independently in its standalone tab; persisting the
    # choice here means every budget read (cold-open GET, live frame, take-over)
    # resolves the window from ONE truthful place instead of the front-end
    # passing the parent/active tab's model id as the denominator (the misalign
    # this column fixes). Stored RAW — including any ``local::`` prefix — so the
    # window resolvers strip it themselves (口径 parity with ``model_hint``).
    # Tail-appended (§3.1, migration 046); ``None`` = no model recorded (existing
    # rows / light callers / unit stubs unaffected — budget readers fall back to
    # their prior id/family default). ``model_provider`` disambiguates an
    # identical ``model_id`` exposed by different providers (e.g. the same
    # ``claude-*`` id under ``provider_a`` 128K vs ``cloud LLM service`` 200K).
    model_id: str | None = None
    model_provider: str | None = None
    # Structured transcript (full-unification rewrite): the sub-agent's turns
    # as first-class :class:`~qai.chat.domain.message.Message` objects — the
    # SAME shape the main agent persists into ``Conversation.messages`` (built
    # by the shared ``build_round_messages`` core). This is the AUTHORITATIVE
    # display + replay source: the detail route serialises it directly (no
    # ``_wire_to_messages`` reverse-fold) and the feed-the-model wire is rebuilt
    # from it via ``rebuild_history_wire_messages`` (口径 parity with the main
    # agent's cross-turn rebuild). Whole-list replaced on each
    # :meth:`record_messages` (the loop owns the canonical ordered list).
    # Tail-appended (§3.1, migration 047); this is the SOLE transcript truth
    # source (SUBAGENT-UNIFY-6 — the legacy ``wire_messages`` field + column
    # were removed in migration 048). Empty list = no structured transcript yet
    # (a freshly ``start()``-ed session / light callers / unit stubs). The
    # system turn is kept here too (role=SYSTEM) so a resume rebuild re-prepends
    # it faithfully.
    messages: list[Message] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.id, SubAgentSessionId):
            raise TypeError(
                "SubAgentSession.id must be SubAgentSessionId, got "
                f"{type(self.id).__name__}",
            )
        if not isinstance(self.root_conversation_id, ConversationId):
            raise TypeError(
                "SubAgentSession.root_conversation_id must be ConversationId, "
                f"got {type(self.root_conversation_id).__name__}",
            )
        if self.parent_subagent_id is not None and not isinstance(
            self.parent_subagent_id,
            SubAgentSessionId,
        ):
            raise TypeError(
                "SubAgentSession.parent_subagent_id must be SubAgentSessionId "
                f"or None, got {type(self.parent_subagent_id).__name__}",
            )
        if not isinstance(self.depth, int) or isinstance(self.depth, bool):
            raise TypeError("SubAgentSession.depth must be an int")
        if self.depth < 1:
            raise ValueError(
                "SubAgentSession.depth must be >= 1 (main agent is depth 0 "
                "and is not persisted as a sub-agent row)",
            )
        if self.parent_message_id is not None and not isinstance(
            self.parent_message_id,
            MessageId,
        ):
            raise TypeError(
                "SubAgentSession.parent_message_id must be MessageId or None, "
                f"got {type(self.parent_message_id).__name__}",
            )
        if not isinstance(self.subagent_type, str):
            raise TypeError("SubAgentSession.subagent_type must be a str")
        if not isinstance(self.title, str):
            raise TypeError("SubAgentSession.title must be a str")
        if not isinstance(self.prompt_preview, str):
            raise TypeError("SubAgentSession.prompt_preview must be a str")
        if len(self.prompt_preview) > _MAX_PROMPT_PREVIEW_LENGTH:
            raise ValueError(
                "SubAgentSession.prompt_preview must be "
                f"<= {_MAX_PROMPT_PREVIEW_LENGTH} chars",
            )
        if not isinstance(self.status, SubAgentSessionStatus):
            raise TypeError(
                "SubAgentSession.status must be SubAgentSessionStatus, got "
                f"{type(self.status).__name__}",
            )
        if not isinstance(self.owner, SubAgentOwner):
            raise TypeError(
                "SubAgentSession.owner must be SubAgentOwner, got "
                f"{type(self.owner).__name__}",
            )
        if not isinstance(self.rounds, int) or isinstance(self.rounds, bool):
            raise TypeError("SubAgentSession.rounds must be an int")
        if self.rounds < 0:
            raise ValueError("SubAgentSession.rounds must be >= 0")
        if not isinstance(self.version, int) or isinstance(self.version, bool):
            raise TypeError("SubAgentSession.version must be an int")
        if self.version < 0:
            raise ValueError("SubAgentSession.version must be >= 0")
        if self.usage is not None and not isinstance(self.usage, dict):
            raise TypeError("SubAgentSession.usage must be a dict or None")
        if self.last_prompt_tokens is not None:
            if not isinstance(self.last_prompt_tokens, int) or isinstance(
                self.last_prompt_tokens, bool
            ):
                raise TypeError(
                    "SubAgentSession.last_prompt_tokens must be an int or None"
                )
            if self.last_prompt_tokens < 0:
                raise ValueError(
                    "SubAgentSession.last_prompt_tokens must be >= 0"
                )
        if not isinstance(self.allow_spawn, bool):
            raise TypeError("SubAgentSession.allow_spawn must be a bool")
        if self.model_id is not None and not isinstance(self.model_id, str):
            raise TypeError("SubAgentSession.model_id must be a str or None")
        if self.model_provider is not None and not isinstance(
            self.model_provider, str
        ):
            raise TypeError(
                "SubAgentSession.model_provider must be a str or None"
            )
        if self.round_snapshots is not None:
            if not isinstance(self.round_snapshots, dict):
                raise TypeError(
                    "SubAgentSession.round_snapshots must be a dict or None"
                )
            for k, v in self.round_snapshots.items():
                if not isinstance(k, int) or isinstance(k, bool):
                    raise TypeError("round_snapshots keys must be int round nos")
                if not isinstance(v, list):
                    raise TypeError("round_snapshots values must be wire lists")
        if not isinstance(self.messages, list):
            raise TypeError("SubAgentSession.messages must be a list")
        for msg in self.messages:
            if not isinstance(msg, Message):
                raise TypeError(
                    "SubAgentSession.messages entries must be Message",
                )
        ensure_aware_utc(self.created_at)
        ensure_aware_utc(self.updated_at)

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------
    @classmethod
    def start(
        cls,
        *,
        session_id: SubAgentSessionId,
        root_conversation_id: ConversationId,
        now: datetime,
        parent_subagent_id: SubAgentSessionId | None = None,
        depth: int = 1,
        parent_message_id: MessageId | None = None,
        subagent_type: str = "agent",
        title: str = "",
        prompt_preview: str = "",
        allow_spawn: bool = False,
        model_id: str | None = None,
        model_provider: str | None = None,
    ) -> SubAgentSession:
        """Construct a brand-new, ``RUNNING`` sub-agent session.

        ``root_conversation_id`` is the top-of-tree main-agent conversation
        (identical for every sub-agent under that root, regardless of depth).
        ``parent_subagent_id`` is the direct-parent sub-agent (``None`` when the
        direct parent is the main agent — a depth-1 sub-agent). ``depth`` is
        the recursion depth (1 = first-level, 2 = grand, …); it must match
        ``parent_subagent_id`` semantically (depth-1 iff parent is None), but
        the invariant is enforced by the caller (the unified ``iter_events``
        spawn path threads the two together — see ``AgentToolHandler``).
        """
        ts = ensure_aware_utc(now)
        return cls(
            id=session_id,
            root_conversation_id=root_conversation_id,
            parent_subagent_id=parent_subagent_id,
            depth=depth,
            parent_message_id=parent_message_id,
            subagent_type=subagent_type,
            title=title,
            prompt_preview=prompt_preview[:_MAX_PROMPT_PREVIEW_LENGTH],
            status=SubAgentSessionStatus.RUNNING,
            owner=SubAgentOwner.MAIN_AGENT,
            rounds=0,
            created_at=ts,
            updated_at=ts,
            allow_spawn=allow_spawn,
            model_id=model_id,
            model_provider=model_provider,
        )

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------
    def record_messages(
        self,
        *,
        messages: list[Message],
        rounds: int | None = None,
        now: datetime,
    ) -> None:
        """Replace the structured transcript with the latest complete list.

        SUBAGENT-UNIFY-6: ``messages`` is the AUTHORITATIVE, *full* ordered
        :class:`Message` list for this run (system turn + per-round
        assistant{tool_calls} + final assistant text), built by the SHARED
        ``build_round_messages`` / ``wire_to_structured_messages`` core — the
        SAME structure the main agent persists into ``Conversation.messages``.
        It is the SOLE transcript truth source (the legacy ``wire_messages`` /
        ``record_round`` track is gone). Stored whole (the loop owns the
        canonical ordering). ``rounds`` (optional) is the absolute round
        counter so far — when supplied it advances :attr:`rounds` (the
        structured-era replacement for ``record_round``'s round bump); ``None``
        leaves the counter untouched (e.g. a take-over that bumps rounds via the
        working-copy constructor). Bumps ``updated_at``.

        Allowed while RUNNING / USER_OWNED; rejected from a settled terminal
        state to surface programming errors.
        """
        if self.status in _TERMINAL_STATUSES:
            raise ValueError(
                f"record_messages() not allowed in terminal status "
                f"{self.status.value}",
            )
        if not isinstance(messages, list):
            raise TypeError("messages must be a list")
        for msg in messages:
            if not isinstance(msg, Message):
                raise TypeError("messages entries must be Message")
        if rounds is not None:
            if not isinstance(rounds, int) or isinstance(rounds, bool):
                raise TypeError("rounds must be an int")
            if rounds < 0:
                raise ValueError("rounds must be >= 0")
            self.rounds = rounds
        self.messages = list(messages)
        self.updated_at = ensure_aware_utc(now)

    def record_round_snapshot(
        self,
        *,
        round_no: int,
        snapshot: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        """Store one round's prompt snapshot (the wire SENT that round).

        ``snapshot`` is the EXACT OpenAI wire list the sub-agent sent the
        model for round ``round_no`` (system + history + per-round
        ``assistant{tool_calls}``/``tool`` blocks), so a standalone sub-agent
        tab's回看 can show a per-round "查看提示词快照" affordance (parity with
        the main agent's per-round snapshot). Whole-dict managed on
        :attr:`round_snapshots` (lazily created); bumps ``updated_at``.

        Allowed while RUNNING / USER_OWNED (the same gate as
        :meth:`record_messages`); rejected from a settled terminal state to
        surface programming errors.
        """
        if self.status in _TERMINAL_STATUSES:
            raise ValueError(
                f"record_round_snapshot() not allowed in terminal status "
                f"{self.status.value}",
            )
        if not isinstance(round_no, int) or isinstance(round_no, bool):
            raise TypeError("round_no must be an int")
        if not isinstance(snapshot, list):
            raise TypeError("snapshot must be a list of wire dicts")
        snap = [entry for entry in snapshot if isinstance(entry, dict)]
        if self.round_snapshots is None:
            self.round_snapshots = {}
        self.round_snapshots[round_no] = snap
        self.updated_at = ensure_aware_utc(now)

    def accumulate_usage(
        self,
        delta: dict[str, Any] | None,
        *,
        now: datetime,
    ) -> None:
        """Add one round's token usage into the cumulative :attr:`usage`.

        ``delta`` is the provider usage dict for ONE round (e.g.
        ``{"prompt_tokens": N, "completion_tokens": M, "total_tokens": K}``);
        ``None`` / non-dict is a no-op. Sums the integer-valued keys into the
        running total (parity with the main agent's ``_accumulate_usage``), so
        the standalone tab can show a cumulative token-usage badge. ALSO updates
        :attr:`last_prompt_tokens` with replace-last semantics from the round's
        ``last_round_prompt_tokens`` (or ``prompt_tokens`` fallback), only when
        it is a positive int — that figure tracks the CURRENT context occupancy
        (the wire we would send next), distinct from the cumulative
        :attr:`usage` sum. Bumps ``updated_at`` when either a usable usage delta
        was folded in OR ``last_prompt_tokens`` was updated. Permitted in any
        non-terminal state and (defensively) terminal too — usage is a pure
        counter, not a state transition.
        """
        if not isinstance(delta, dict):
            return
        folded = False
        current = dict(self.usage) if isinstance(self.usage, dict) else {}
        for key, value in delta.items():
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            current[key] = int(current.get(key, 0)) + value
            folded = True
        if folded:
            self.usage = current
        # Replace-last: track the most recent round's true wire size. Prefer
        # ``last_round_prompt_tokens`` (the turn's last-round wire); for raw
        # per-round sub-agent extracts that only carry ``prompt_tokens`` (a
        # single-stream round IS its own last round) use that as the fallback.
        _lrp = delta.get("last_round_prompt_tokens")
        if _lrp is None:
            _lrp = delta.get("prompt_tokens")
        try:
            _lrp_i = int(_lrp) if _lrp is not None else 0
        except (TypeError, ValueError):
            _lrp_i = 0
        updated_last = False
        if _lrp_i > 0:
            self.last_prompt_tokens = _lrp_i
            updated_last = True
        if folded or updated_last:
            self.updated_at = ensure_aware_utc(now)

    def set_model(
        self,
        model_id: str | None,
        model_provider: str | None,
        *,
        now: datetime,
    ) -> None:
        """Set this sub-agent's OWN model (the budget-denominator真值源).

        Used when the user switches THIS sub-agent's model in its standalone
        tab. Per the拍板 design this changes ONLY the budget denominator (the
        window the front-end divides by) — it does NOT touch
        :attr:`last_prompt_tokens` (the历史实测 numerator), which stays the
        provider-measured wire size of the last round actually run (used is
        historical truth, not re-derived from a model swap). Stores ``model_id``
        RAW (any ``local::`` prefix preserved — readers strip it). Permitted in
        any state (a user may re-point the model of a settled session before
        taking it over); bumps :attr:`updated_at` (mirrors the other mutators'
        now / updated_at范式).
        """
        if model_id is not None and not isinstance(model_id, str):
            raise TypeError("model_id must be a str or None")
        if model_provider is not None and not isinstance(model_provider, str):
            raise TypeError("model_provider must be a str or None")
        self.model_id = model_id
        self.model_provider = model_provider
        self.updated_at = ensure_aware_utc(now)

    def mark_done(self, *, rounds: int, now: datetime) -> None:
        """Settle the session as ``DONE`` (normal completion).

        Idempotent if already ``DONE``.  Rejected from another terminal
        state to surface programming errors.
        """
        if self.status is SubAgentSessionStatus.DONE:
            return
        self._guard_settle("mark_done")
        if not isinstance(rounds, int) or isinstance(rounds, bool):
            raise TypeError("rounds must be an int")
        if rounds < 0:
            raise ValueError("rounds must be >= 0")
        self.status = SubAgentSessionStatus.DONE
        self.rounds = rounds
        self.updated_at = ensure_aware_utc(now)

    def mark_error(self, *, now: datetime) -> None:
        """Settle the session as ``ERROR`` (failed)."""
        if self.status is SubAgentSessionStatus.ERROR:
            return
        self._guard_settle("mark_error")
        self.status = SubAgentSessionStatus.ERROR
        self.updated_at = ensure_aware_utc(now)

    def mark_interrupted(self, *, now: datetime) -> None:
        """Settle the session as ``INTERRUPTED`` (aborted)."""
        if self.status is SubAgentSessionStatus.INTERRUPTED:
            return
        self._guard_settle("mark_interrupted")
        self.status = SubAgentSessionStatus.INTERRUPTED
        self.updated_at = ensure_aware_utc(now)

    def take_over_by_user(self, *, now: datetime) -> None:
        """Hand the session to the user (``owner=USER``, ``status=USER_OWNED``).

        Allowed from any non-terminal state and idempotent once already
        user-owned.  A user may not take over a session that has already
        settled terminally (DONE / ERROR / INTERRUPTED).

        SHARED ownership: a user take-over does NOT lock the main agent out.
        After this, the user may converse with the session (each turn via
        :meth:`record_messages`, still permitted in USER_OWNED), AND the main
        agent may later WAKE it again (the resume path rebuilds a fresh
        RUNNING working copy from the persisted structured transcript via
        ``rebuild_history_wire_messages``) to read the latest conclusion the
        user reached with the sub-agent. ``owner`` is a hint about who most
        recently drove it, not an exclusive lock.
        """
        if (
            self.status is SubAgentSessionStatus.USER_OWNED
            and self.owner is SubAgentOwner.USER
        ):
            return
        if self.status in _TERMINAL_STATUSES:
            raise ValueError(
                f"take_over_by_user() not allowed from terminal status "
                f"{self.status.value}",
            )
        self.owner = SubAgentOwner.USER
        self.status = SubAgentSessionStatus.USER_OWNED
        self.updated_at = ensure_aware_utc(now)

    # ------------------------------------------------------------------
    # Predicates
    # ------------------------------------------------------------------
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def is_user_owned(self) -> bool:
        return self.owner is SubAgentOwner.USER

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _guard_settle(self, attempted: str) -> None:
        """Reject settling when already in a *different* terminal state."""
        if self.status in _TERMINAL_STATUSES:
            raise ValueError(
                f"{attempted}() not allowed from terminal status "
                f"{self.status.value}",
            )


__all__ = [
    "SubAgentSession",
    "SubAgentSessionStatus",
    "SubAgentOwner",
]
