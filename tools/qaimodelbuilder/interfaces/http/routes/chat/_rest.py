# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Chat REST routes (JSON, no streaming) — PR-033 stage A + PR-042 + PR-043.

Mounted under ``/api/chat``. The aggregate router lives in
``__init__.py``; this file holds the non-streaming endpoints.

Routes covered:

* ``POST   /api/chat/conversations``               create conversation
* ``GET    /api/chat/conversations``               list / search
* ``GET    /api/chat/conversations/{id}``          single conversation summary
* ``GET    /api/chat/conversations/{id}/messages`` paged messages
* ``GET    /api/chat/conversations/{id}/context``  context-size estimate
* ``PATCH  /api/chat/conversations/{id}``          rename
* ``PATCH  /api/chat/conversations/{id}/budget``   set token budget cap
* ``DELETE /api/chat/conversations/{id}``          delete
* ``POST   /api/chat/conversations/{id}/compact``  compaction decision
* ``POST   /api/chat/stop``                        stop in-flight stream
                                                   (idempotent; body-keyed by
                                                   ``tab_id``)
* ``POST   /api/chat/experiences``                 save experience (PR-042)
* ``GET    /api/chat/experiences``                 list experiences (PR-042)
* ``GET    /api/chat/experiences/categories``      list categories (PR-042)
* ``DELETE /api/chat/experiences/{id}``            delete experience (PR-042)
* ``POST   /api/chat/tabs``                        open tab (PR-043)
* ``GET    /api/chat/tabs``                        list active tabs (PR-043)
* ``DELETE /api/chat/tabs/{tab_id}``               close tab (PR-043)

The streaming entry-point (``POST /api/chat`` in the legacy system)
is split off into ``_sse.py`` (``GET /api/chat/conversations/{id}/stream``)
and ``_ws.py`` (``WS /api/chat/ws``) per refactor-plan §10.4.

PR-043 closes the last PR-033 coordination request: tab CRUD now goes
through ``OpenTabUseCase`` / ``CloseTabUseCase`` / ``ListActiveTabsUseCase``
instead of the SSE / WS handlers calling ``ConversationTab.open(...)``
directly. The handlers still auto-provision tabs on demand for
existing query-string clients (see ``_sse._resolve_or_create_tab``)
through the same use-case path.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from qai.chat.application.use_cases.compact import CompactChatInput
from qai.chat.application.use_cases.conversation_management import (
    CreateConversationInput,
    DeleteConversationInput,
    GetConversationMessagesInput,
    ListConversationsInput,
    RenameConversationInput,
    SetConversationFavoriteInput,
    SetConversationPinnedInput,
    SetConversationWorkspaceInput,
)
from qai.chat.application.use_cases.experience_management import (
    DeleteExperienceInput,
    ListExperiencesInput,
    SaveExperienceInput,
)
from qai.chat.application.use_cases.implementation_plan import (
    plan_to_dict,
    read_implementation_plan,
)
from qai.chat.application.use_cases.participant_management import (
    CreateParticipantInput,
    DeleteParticipantInput,
    GetDiscussionConfigInput,
    GetImplementationPlanInput,
    ListParticipantsInput,
    SetDiscussionConfigInput,
    UpdateImplementationPlanInput,
    UpdateParticipantInput,
)
from qai.chat.application.use_cases.streaming import StopChatInput
from qai.chat.application.use_cases.streaming import CancelToolInput
from qai.chat.application.use_cases.tab_management import (
    CloseTabInput,
    OpenTabInput,
)
from qai.chat.domain.ids import (
    ConversationId,
    ParticipantId,
    SubAgentSessionId,
    TabId,
)
from qai.chat.domain.content import compute_context_usage
from qai.chat.domain.model_profiles import get_context_limit
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


_log = get_logger(__name__)


# ---- Request / Response DTOs ---------------------------------------------


class CreateConversationRequest(BaseModel):
    """``POST /api/chat/conversations`` body."""

    title: str = Field(..., min_length=1, max_length=256)


class ConversationSummary(BaseModel):
    """Single conversation projection (no message history)."""

    id: str
    title: str
    status: str
    created_at: str
    updated_at: str
    message_count: int
    # V1-parity sidebar badges (round = user turns, tool calls). Appended
    # fields; default 0 keeps the response shape backward-compatible.
    round_count: int = 0
    tool_call_count: int = 0
    # Sub-agent count (number of sub-agent sessions spawned under this
    # conversation). Appended with default 0 (AGENTS.md §3.1); lets the
    # sidebar show the "expand sub-agents" arrow ONLY when > 0.
    subagent_count: int = 0
    # V1-parity channel source metadata (SidebarPanel.js:116-132).
    # Stores e.g. {"source": "wechat"} for channel conversations; None for
    # web-UI conversations.  Appended with default None (AGENTS.md §3.1).
    meta: dict | None = None
    # Pin / favorite flags (persisted in meta.pinned / meta.favorite). Exposed
    # as top-level booleans so the sidebar can pin conversations on top and the
    # favorites dialog can filter them without re-reading meta. Appended with
    # default False (AGENTS.md §3.1).
    pinned: bool = False
    favorite: bool = False
    # Promote-ready turn-end detection result (migration 057). The backend
    # detects at turn end (StreamChatUseCase._finalize_assistant_message) and
    # persists it onto Conversation.detected_model; the frontend "Promote to
    # App Builder" CTA reads it here with ZERO on-open disk scans. Shape:
    #   {"workdir": str, "variants": [{"precision": str, "label": str}...],
    #    "checked_at": "<iso8601>"}
    # None = never detected (legacy / forward-compatible). Appended with
    # default None (AGENTS.md §3.1); older frontends ignore the new field.
    detected_model: dict | None = None


class ConversationListResponse(BaseModel):
    """``GET /api/chat/conversations`` body."""

    items: list[ConversationSummary]


class ActiveRunStopSpec(BaseModel):
    """Stop action descriptor for one active run."""

    method: str = "POST"
    path: str
    body: dict[str, Any]


class ActiveRunItem(BaseModel):
    """Unified active ordinary-chat / sub-agent run projection."""

    kind: str
    id: str
    tab_id: str | None = None
    conversation_id: str | None = None
    subagent_id: str | None = None
    # Alpha unified-spawn-path (migration 049): the ROOT conversation of a
    # sub-agent run. Tail-appended (§3.1). ``None`` for non-sub-agent kinds
    # (a plain chat run has no sub-agent tree). Historical note: an earlier
    # ``parent_conversation_id`` alias was carried alongside this in stage
    # α; it was removed in β (front-end fully migrated to
    # ``root_conversation_id``).
    root_conversation_id: str | None = None
    title: str | None = None
    status: str | None = None
    model_id: str | None = None
    model_provider: str | None = None
    started_at: str
    last_active_at: str
    aborted: bool = False
    reason: str | None = None
    openable: bool = True
    attach_path: str | None = None
    stop: ActiveRunStopSpec


class ActiveRunsResponse(BaseModel):
    """``GET /api/chat/active-runs`` body."""

    items: list[ActiveRunItem]


class MessageItem(BaseModel):
    """Single message projection."""

    id: str
    role: str
    text: str
    created_at: str
    parent_id: str | None
    # Appended field (AGENTS.md §3.1): tool-call cards for agentic turns.
    # Each entry is a ChatToolCall-shaped dict: {id, tool, args, output?,
    # status, isError?, outputSize?, truncated?}.  None / absent when the
    # message has no tool calls (backward-compatible default).
    tool_calls: list[dict[str, Any]] | None = None
    # Appended field (AGENTS.md §3.1): assistant-turn token usage, mirroring
    # the terminal stream `end` frame's `usage` dict (normalized OpenAI shape:
    # {prompt_tokens, completion_tokens, total_tokens, cache_read_tokens?,
    # cache_write_tokens?}).  Lets the client re-render the token line after a
    # page reload (P1-4).  None / absent when the turn carried no usage.
    usage: dict[str, Any] | None = None
    # Appended fields (AGENTS.md §3.1): the model that produced an assistant
    # turn (V1 parity: msg.model_id / msg.model_provider). Lets the client
    # show the real model name in a reloaded history bubble even after the
    # user switches models. None / absent for non-assistant / legacy rows.
    model_id: str | None = None
    model_provider: str | None = None
    # Appended field (AGENTS.md §3.1): the discussion participant that authored
    # an assistant turn (V2 multi-agent: msg.sender_id = participant id). Lets a
    # reloaded history bubble re-show the speaker's avatar colour + role name by
    # matching this id against the conversation's participant roster (the live
    # stream gets it from the `speaker_changed` frame; history has no such frame
    # so it must travel on the message). None / absent for single-agent / legacy
    # rows.
    sender_id: str | None = None
    # Appended field (AGENTS.md §3.1): V1-parity render-extras envelope so a
    # history reload re-shows everything the live stream rendered (V1
    # backend/history_store.py:_row_to_message promotes these from
    # messages.meta). Keys: request_id (prompt-snapshot button), image_url
    # (image preview), perf ({ttft_ms, total_ms, ...} perf line),
    # subAgentBlocks (sub-agent fold blocks), tool_full_output / tool_truncated
    # / tool_output_size (tool-truncation badge + full-output tab). None /
    # absent when the turn carried no extras.
    meta: dict[str, Any] | None = None


class MessagesPageResponse(BaseModel):
    """``GET /api/chat/conversations/{id}/messages`` body."""

    items: list[MessageItem]
    next_cursor: str | None


class RenameConversationRequest(BaseModel):
    """``PATCH /api/chat/conversations/{id}`` body."""

    title: str = Field(..., min_length=1, max_length=256)


class SetConversationWorkspaceRequest(BaseModel):
    """``PATCH /api/chat/conversations/{id}/workspace`` body.

    ``workspace`` is the per-session working directory for the agent's
    file / exec tools. ``None`` or a blank string clears the per-session
    override so the conversation falls back to the global configured
    workspace.
    """

    workspace: str | None = Field(default=None, max_length=4096)


class SetConversationPinnedRequest(BaseModel):
    """``PATCH /api/chat/conversations/{id}/pin`` body.

    ``pinned`` toggles whether the conversation is pinned to the top of the
    sidebar list (persisted in ``meta.pinned``).
    """

    pinned: bool


class SetConversationFavoriteRequest(BaseModel):
    """``PATCH /api/chat/conversations/{id}/favorite`` body.

    ``favorite`` toggles whether the conversation appears in the favorites
    library dialog (persisted in ``meta.favorite``).
    """

    favorite: bool


class SetConversationBudgetRequest(BaseModel):
    """``PATCH /api/chat/conversations/{id}/budget`` body.

    ``max_tokens`` sets the per-conversation TOKEN-budget cap (renamed from the
    CC SDK's ``max_budget_usd`` — this project has no cross-provider USD
    pricing but has accurate provider usage counts). ``None`` (or omitted)
    DISABLES the budget for the conversation (the default). A positive value
    enables enforcement at each agentic-round boundary; a value <= 0 is
    normalised to disabled by the tracker. ``reset_used`` (optional) zeroes the
    running ``used_tokens`` counter so the user gets a fresh accounting window
    when raising the cap.
    """

    max_tokens: int | None = Field(default=None, ge=0)
    reset_used: bool = False


class ConversationBudgetResponse(BaseModel):
    """``PATCH /api/chat/conversations/{id}/budget`` response.

    Mirrors :class:`~qai.chat.domain.budget.BudgetCheckResult`: the effective
    cap (``max_tokens`` — ``None`` = disabled), the running ``used_tokens``
    counter, ``exceeded`` (``used >= max_tokens`` when enabled), and the derived
    ``remaining`` (``None`` = unbounded).
    """

    max_tokens: int | None = None
    used_tokens: int = 0
    exceeded: bool = False
    remaining: int | None = None
    enabled: bool = False


class CompactChatRequest(BaseModel):
    """``POST /api/chat/conversations/{id}/compact`` body."""

    budget_tokens: int = Field(..., gt=0)
    trigger_threshold: float = Field(0.8, ge=0.0, le=1.0)


class ContextSizeResponse(BaseModel):
    """``POST /api/chat/conversations/{id}/compact`` body
    (also ``GET /api/chat/conversations/{id}/context``)."""

    used_tokens: int
    budget_tokens: int
    ratio: float
    needs_compaction: bool
    # Appended fields (AGENTS.md §3.1): compression-state badge support for
    # GET /context. ``used_tokens`` keeps its original meaning = the
    # PRE-compaction full-history token count (system prompt + the entire
    # rebuilt ``conv.messages``). These two report the POST-compaction state:
    #
    # * ``compacted`` — ``True`` when the conversation has an active session
    #   compaction checkpoint (it was compressed); ``False`` otherwise.
    # * ``compacted_tokens`` — the SAME-口径 token count of the wire actually
    #   sent to the model now (compacted head + verbatim increment), strictly
    #   ``< used_tokens`` when compressed; ``None`` when not compacted.
    #
    # Both default to the un-compacted state so the shared /compact response
    # (which never populates them) stays backward-compatible.
    compacted_tokens: int | None = None
    compacted: bool = False
    # Appended fields (AGENTS.md §3.1): the REAL (un-clamped) pre-compaction
    # occupancy. ``used_tokens`` / ``ratio`` are clamped to the window to honour
    # the ``ContextSize`` domain invariant (budget >= used), so they floor at
    # the window (200K / 100%) and cannot reveal an over-window history. These
    # carry the truth for the badge: ``raw_used_tokens`` may exceed
    # ``budget_tokens`` and ``raw_ratio`` may exceed 1.0 (e.g. 1.11 = 111%),
    # which is exactly the "history is over the window, compaction imminent"
    # signal the UI should show. Default to the clamped values via the endpoint
    # so older/other callers (the shared /compact response) stay consistent.
    raw_used_tokens: int = 0
    raw_ratio: float = 0.0


class StopChatRequest(BaseModel):
    """``POST /api/chat/stop`` body — body-keyed (NOT path-keyed) on tab id
    so we never embed an opaque id in a URL when the tab may already
    have been closed.
    """

    tab_id: str = Field(..., min_length=1, max_length=64)
    reason: str = Field("user_requested", min_length=1, max_length=64)


class StopChatResponse(BaseModel):
    """Outcome of ``POST /api/chat/stop`` — idempotent."""

    aborted: bool
    reason: str


class CancelToolRequest(BaseModel):
    """``POST /api/chat/cancel_tool`` body — cancel ONE running tool call.

    Body-keyed on ``tab_id`` + the tool's ``call_id``. Unlike ``/stop`` this
    does NOT abort the turn; the backend synthesizes a ``[cancelled]`` result
    for that one tool and the turn continues.
    """

    tab_id: str = Field(..., min_length=1, max_length=64)
    call_id: str = Field(..., min_length=1, max_length=128)


class CancelToolResponse(BaseModel):
    """Outcome of ``POST /api/chat/cancel_tool`` — idempotent."""

    cancelled: bool
    call_id: str


class AnswerQuestionRequest(BaseModel):
    """``POST /api/chat/answer`` body — delivers the user's answer to a
    blocking ``question`` tool call.

    Body-keyed on ``tab_id`` (NOT path-keyed): a tab can only ever have one
    pending question at a time (its agentic loop is suspended on the answer),
    so the tab id is sufficient to correlate the answer with the awaiting
    tool — no server-minted question id needs to round-trip through the
    (locked) ``tool_call`` frame.
    """

    tab_id: str = Field(..., min_length=1, max_length=64)
    answer: str = Field(..., max_length=100_000)


class AnswerQuestionResponse(BaseModel):
    """Outcome of ``POST /api/chat/answer`` — idempotent.

    ``delivered`` is ``False`` when there was no pending question for the tab
    (already answered / timed out / stream aborted); the front-end treats this
    as a benign no-op (it may race the question's timeout).
    """

    delivered: bool


# ---- Tool catalogue DTOs (2026-06-21 — Discussion role allowlist UI) ----


class ChatToolDescriptor(BaseModel):
    """Public descriptor for one chat-registered tool.

    Surfaced by ``GET /api/chat/tools`` so the front-end (Discussion role
    panel + future "what can this role do?" inspectors) can render the live
    set of available tools without baking the list into the front-end build
    — when a new tool is registered in the chat tool registry it appears
    automatically (judgement 1: single source of truth = the live registry).
    """

    name: str
    """Tool name (registry key)."""
    description: str
    """One-line tool description (taken verbatim from the OpenAI
    function-call schema's ``function.description`` — same text the LLM
    sees when deciding whether to use the tool)."""
    available_in_discussion: bool
    """``True`` when this tool may be exposed to a discussion speaker (the
    speaker still must list it in ``allowed_tools``). ``False`` for tools
    the back-end hard-blocks for discussions (currently none — but the
    back-end keeps :data:`_DISCUSSION_EXCLUDED_TOOLS` as a future-proof
    hook). The front-end uses this flag to grey-out / annotate the tool
    chip rather than hide it."""
    conditional: bool
    """``True`` for mode-conditional tools (``appbuilder_run`` /
    ``appbuilder_batch_run``) that only register schemas in certain tool
    modes. The front-end shows them with a tooltip explaining the gating."""


class ListChatToolsResponse(BaseModel):
    """Outcome of ``GET /api/chat/tools``.

    ``tools`` is ordered by the chat adapter's canonical ``TOOL_ORDER``
    (read / edit / write / apply_patch / exec / background_process /
    glob / grep / webfetch / web_search / agent / list_subagents /
    skill / todowrite / question / appbuilder_run / appbuilder_batch_run)
    — the single source of truth for display order across all front-end
    surfaces.
    """

    tools: list[ChatToolDescriptor]


# ---- Experience DTOs (PR-042) -------------------------------------------


class SaveExperienceRequest(BaseModel):
    """``POST /api/chat/experiences`` body.

    ``experience_id=None`` mints a fresh id (create); a non-None id
    upserts an existing record (update).
    """

    category: str = Field(..., min_length=1, max_length=64)
    content: str = Field(..., min_length=1, max_length=100_000)
    metadata: dict[str, Any] | None = Field(default=None)
    experience_id: str | None = Field(default=None, max_length=64)


class ExperienceItem(BaseModel):
    """Single experience projection."""

    id: str
    category: str
    content: str
    metadata: dict[str, Any]
    created_at: str


class ExperienceListResponse(BaseModel):
    """``GET /api/chat/experiences`` body."""

    items: list[ExperienceItem]


class ExperienceCategoriesResponse(BaseModel):
    """``GET /api/chat/experiences/categories`` body."""

    categories: list[str]


# ---- Tab DTOs (PR-043) --------------------------------------------------


class OpenTabRequest(BaseModel):
    """``POST /api/chat/tabs`` body.

    ``tab_id=None`` mints a fresh id (the common case from front-end
    "open new tab"); a non-None id idempotently re-opens an existing
    tab record (e.g. after a page reload that already owns a TabId).
    """

    conversation_id: str = Field(..., min_length=1, max_length=64)
    tab_id: str | None = Field(default=None, min_length=1, max_length=64)


class TabItem(BaseModel):
    """Single :class:`ConversationTab` projection."""

    id: str
    conversation_id: str
    status: str
    created_at: str
    last_active_at: str


class TabListResponse(BaseModel):
    """``GET /api/chat/tabs`` body."""

    items: list[TabItem]


# ---- Sub-agent DTOs ------------------------------------------------------


class SubAgentSummary(BaseModel):
    """Lightweight :class:`SubAgentSession` projection for list views.

    Omits the structured message list to keep the history-fold payload
    small; the full messages transcript is only materialised by the detail
    endpoint (returned as ``messages`` — persisted as ``messages_json``
    since migrations 047/048).
    """

    subagent_id: str
    parent_message_id: str | None
    subagent_type: str
    title: str
    prompt_preview: str
    status: str
    owner: str
    rounds: int
    created_at: str
    updated_at: str
    # NEW (sub-agent model真值源, migration 046): the sub-agent's OWN model id +
    # provider — the single source of truth for the context-budget denominator.
    # Tail-appended (§3.1). ``None`` when the run recorded no model (legacy
    # rows). Stored RAW (any ``local::`` prefix preserved).
    model_id: str | None = None
    model_provider: str | None = None
    # Tree edges of this sub-agent (unified spawn-path, migration 049;
    # tail-appended §3.1).
    #   * ``root_conversation_id`` — the ROOT (top-of-tree main-agent) conversation
    #     id; identical for every sub-agent under that root, regardless of depth.
    #     This is the honest name for what pre-α called ``parent_conversation_id``
    #     (a name removed in β — every row is now keyed by root + parent_subagent_id
    #     + depth, so a straight "parent conversation" is ambiguous once the tree
    #     is nested).
    #   * ``parent_subagent_id`` — DIRECT-parent sub-agent id. ``None`` = the
    #     direct parent is the main agent (a depth-1 sub-agent under the root
    #     conversation); non-``None`` = the direct parent is another sub-agent
    #     row (a grand / great-grand / … cell in the tree).
    #   * ``depth`` — recursion depth (1 = first-level, 2 = grand, 3 = great-
    #     grand, …). Defaults to 1 so legacy rows / light callers unaffected.
    root_conversation_id: str = ""
    parent_subagent_id: str | None = None
    depth: int = 1


class SubAgentListResponse(BaseModel):
    """``GET /api/chat/conversations/{id}/subagents`` body."""

    items: list[SubAgentSummary]


class SubAgentMessageItem(BaseModel):
    """A single rendered turn restored from a sub-agent's wire history.

    Display-only projection of one OpenAI wire dict (system turns dropped,
    multimodal content coerced to text) so the front-end can reuse its
    standard message renderer when opening a sub-agent in a new tab.
    """

    role: str
    text: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    # Per-turn display timestamp (ISO 8601) when the sub-agent loop recorded
    # this turn — lets the front-end show the REAL time each turn occurred
    # instead of synthesising all turns from one base (which made every message
    # show the sub-agent's start/last-update time). Optional: absent for legacy
    # persisted wire that predates per-turn timestamps. Tail-appended (§3.1).
    created_at: str | None = None
    # 回看 parity with the main agent's ``MessageItem`` (tail-appended §3.1): an
    # assistant turn carries that round's token ``usage`` (per-message token
    # line) + ``meta.request_id`` (per-message 📄 prompt-snapshot button) so the
    # front-end reuses its STANDARD ``mapHistoryItems`` path — IDENTICAL
    # per-message usage badge + snapshot button as the main agent, no sub-agent-
    # specific render. Populated from the per-round request_id/usage the
    # sub-agent loop stamps onto the persisted wire. ``None`` / absent for
    # non-assistant turns / runs that recorded none (older data, no store).
    usage: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None


class SubAgentDetailResponse(BaseModel):
    """``GET /api/chat/subagents/{id}`` body."""

    subagent_id: str
    status: str
    owner: str
    subagent_type: str
    title: str
    prompt_preview: str
    rounds: int
    created_at: str
    updated_at: str
    messages: list[SubAgentMessageItem]
    # NEW (sub-agent context-badge fix): the sub-agent's OWN context usage,
    # estimated server-side from its persisted ``wire_messages`` with the same
    # BPE-free chars/4 estimate the main context badge uses, and
    # ``get_context_limit`` for the budget. Tail-appended (§3.1). A standalone
    # sub-agent tab carries its PARENT's conversation id, so the per-conversation
    # /context endpoint would show the parent's usage — these fields let the
    # front-end badge report the sub-agent's own usage instead. ``ratio`` is
    # clamped to [0, 1].
    used_tokens: int = 0
    budget_tokens: int = 200_000
    ratio: float = 0.0
    # Appended fields (AGENTS.md §3.1): the REAL (un-clamped) occupancy, same口径
    # as GET /context's ``raw_used_tokens`` / ``raw_ratio``. ``used_tokens`` /
    # ``ratio`` above are clamped to the window (floor at 200K / 100%); these
    # carry the truth so the sub-agent badge can surface an over-window state
    # (``raw_ratio`` > 1.0 = "compaction imminent"), at parity with the main
    # agent badge. Default to the clamped values via the endpoint so other
    # callers stay consistent.
    raw_used_tokens: int = 0
    raw_ratio: float = 0.0
    # NEW (sub-agent回看 parity, migration 035): the sub-agent run's CUMULATIVE
    # token usage summed across every round (provider usage dict, e.g.
    # {"prompt_tokens", "completion_tokens", "total_tokens"}) so a standalone
    # tab can show a token-usage badge mirroring the main agent. Tail-appended
    # (§3.1); ``None`` when the run recorded no usage. Distinct from
    # ``used_tokens`` above (which is the CURRENT context-window estimate from
    # the persisted wire) — this is the LIFETIME tokens the run consumed.
    token_usage: dict[str, Any] | None = None
    # NEW (sub-agent回看 parity, migration 035): per-round prompt snapshots —
    # maps a round number (string key) to that round's EXACT sent wire list, so
    # a standalone tab can offer a per-round "查看提示词快照" affordance like the
    # main agent. Tail-appended (§3.1); ``None`` when none were captured.
    round_snapshots: dict[str, list[dict[str, Any]]] | None = None
    # NEW (sub-agent spawn-permission, migration 045): whether this sub-agent
    # was GRANTED the ability to create its own sub-agents at spawn time (the
    # spawning main agent's ``allow_child_spawn`` switch was ON). Tail-appended
    # (§3.1). The front-end uses it to DEFAULT the take-over tab's "allow this
    # sub-agent to create sub-agents" toggle ON for an authorised sub-agent
    # (the user may still turn it off — a default, not a lock). Default False =
    # not granted (the historical hard recursion guard).
    allow_spawn: bool = False
    # Sub-agent model真值源 (migration 046): the sub-agent's OWN model id +
    # provider — the single source of truth for the budget denominator
    # (``budget_tokens`` above is resolved from THIS model, not the parent/active
    # tab's). Tail-appended (§3.1). The front-end shows the sub-agent tab's model
    # selector pre-set to this value and PATCHes it back when the user switches.
    # ``None`` when the run recorded no model (legacy rows). Stored RAW (any
    # ``local::`` prefix preserved).
    model_id: str | None = None
    model_provider: str | None = None
    # Tree edges of this sub-agent (unified spawn-path, migration 049;
    # tail-appended §3.1). See ``SubAgentSummary`` for the field-by-field
    # semantics. Historical note: an earlier ``parent_conversation_id`` alias
    # was carried alongside these in stage α; it was removed in β (front-end
    # fully migrated to ``root_conversation_id``).
    root_conversation_id: str = ""
    parent_subagent_id: str | None = None
    depth: int = 1


class SubAgentInterruptResponse(BaseModel):
    """``POST /api/chat/subagents/{id}/interrupt`` body.

    ``ok`` is ``True`` whenever the session was found and the request was
    processed (idempotent). ``aborted`` (block 3) reports whether a running
    sub-agent's independent cancellation flag was actually signalled — it is
    ``True`` only when the sub-agent was in-flight (a flag was registered),
    ``False`` when it had already finished (idempotent no-op).
    """

    ok: bool
    aborted: bool


class UpdateSubAgentModelRequest(BaseModel):
    """``PATCH /api/chat/subagents/{id}`` body — set the sub-agent's own model.

    ``model_id`` is the model the user selected for THIS sub-agent in its
    standalone tab (the budget-denominator真值源, migration 046); stored RAW
    (any ``local::`` prefix preserved — the window resolvers strip it).
    ``model_provider`` disambiguates an identical ``model_id`` exposed by
    different providers (optional; ``None`` → id-only catalog lookup). Per the
    拍板 design this changes ONLY the budget denominator — the sub-agent's
    ``used`` (last-round实测 numerator) is left untouched.
    """

    model_id: str
    model_provider: str | None = None


# ---- Multi-agent discussion DTOs (block 4) -------------------------------


class ParticipantConfig(BaseModel):
    """Per-participant discussion config blob (``chat_participant.config_json``).

    ``allowed_tools`` is the tool set this named-agent role may invoke in a
    discussion (empty / absent → a text-only discussant); ``color`` is a
    theme-palette token for the bubble (never a hard-coded colour);
    ``enabled_skills`` is the SKILL whitelist for this role (absent / empty →
    no skill: the role gets no ``skill`` tool and no skill catalog). Selecting
    skills is opt-in per role — a role has no skill unless explicitly granted.
    """

    allowed_tools: list[str] | None = Field(default=None)
    color: int | str | None = Field(default=None)
    enabled_skills: list[str] | None = Field(default=None)

    def to_blob(self) -> dict[str, Any] | None:
        """Project to the aggregate's config dict (``None`` when empty)."""
        blob: dict[str, Any] = {}
        if self.allowed_tools is not None:
            blob["allowed_tools"] = list(self.allowed_tools)
        if self.color is not None:
            blob["color"] = self.color
        if self.enabled_skills is not None:
            blob["enabled_skills"] = list(self.enabled_skills)
        return blob or None


class ParticipantItem(BaseModel):
    """Single :class:`Participant` projection (discussion roster row)."""

    id: str
    conversation_id: str
    kind: str
    display_name: str
    model_id: str | None = None
    persona: str | None = None
    config: dict[str, Any] | None = None
    created_at: str
    updated_at: str


class ParticipantListResponse(BaseModel):
    """``GET /api/chat/conversations/{id}/participants`` body."""

    items: list[ParticipantItem]


class CreateParticipantRequest(BaseModel):
    """``POST /api/chat/conversations/{id}/participants`` body."""

    display_name: str = Field(default="", max_length=256)
    model_id: str | None = Field(default=None, max_length=256)
    persona: str | None = Field(default=None, max_length=100_000)
    config: ParticipantConfig | None = Field(default=None)


class UpdateParticipantRequest(BaseModel):
    """``PATCH /api/chat/conversations/{id}/participants/{pid}`` body.

    PATCH semantics: only the fields the client SENDS are applied; an omitted
    field is left unchanged, an explicit ``null`` clears the (nullable) field.
    Field-presence is read from ``model_fields_set`` by the handler.
    """

    display_name: str | None = Field(default=None, max_length=256)
    model_id: str | None = Field(default=None, max_length=256)
    persona: str | None = Field(default=None, max_length=100_000)
    config: ParticipantConfig | None = Field(default=None)


class DiscussionConfigBody(BaseModel):
    """``GET`` / ``PATCH /api/chat/conversations/{id}/discussion`` body.

    Mirrors ``Conversation.meta["discussion"]``. ``is_discussion`` is tri-state
    on the PATCH request (MERGE semantics):

    * ``True``  — enable discussion mode and merge the supplied switches;
    * ``False`` — explicitly clear the whole discussion config;
    * omitted / ``null`` — keep the current on/off state and merge only the
      supplied switches (the real front-end's single-key partial PATCH, so a
      toggle of ONE switch never wipes the others).

    The GET / PATCH **response** always coerces ``is_discussion`` to a real bool
    (§3.1 response-shape lock — the request body may be null, the response never
    is).
    """

    is_discussion: bool | None = None
    selector_mode: str | None = Field(default=None, max_length=64)
    max_rounds: int | None = Field(default=None, ge=1, le=1000)
    enable_judge: bool | None = Field(default=None)
    discussion_prompt: str | None = Field(default=None, max_length=8000)
    # Collaboration-mode V1 (§26/§27) — tail-appended (§3.1). The selected mode
    # id + how it was picked (auto/manual/locked/suggested).
    selected_mode_id: str | None = Field(default=None, max_length=64)
    mode_selection_policy: str | None = Field(default=None, max_length=32)
    # DISC-2 二期 convergence-control flags — tail-appended (§3.1). ``None``
    # leaves any existing value untouched (PATCH semantics); a missing key in
    # the persisted meta resolves to OFF at orchestrator read time.
    convergence_control_enabled: bool | None = Field(default=None)
    manager_early_end_enabled: bool | None = Field(default=None)
    soft_stop_enabled: bool | None = Field(default=None)
    soft_stop_mode: str | None = Field(default=None, max_length=32)
    # DISC-2 P4-step1 (§22A.7) — social/lightweight-path response policy,
    # tail-appended (§3.1). ``None`` leaves any existing value untouched (PATCH
    # semantics); a missing/illegal key resolves to ``single_brief_reply`` (the
    # phase-1 behaviour) at orchestrator read time.
    social_response_policy: str | None = Field(default=None, max_length=32)
    # DISC-2 P4-step2 (§22A.7, final step) — Manager prompt customization,
    # tail-appended (§3.1). ``None`` leaves any existing value untouched (PATCH
    # semantics). ``manager_prompt_customization_mode`` ∈ {none, append_instruction,
    # advanced_override}; ``advanced_override`` is RESERVED (not open in the MVP).
    # The front-end may send only ``manager_prompt_append`` (a non-empty append
    # with no mode implies ``append_instruction`` at orchestrator read time).
    manager_prompt_customization_mode: str | None = Field(
        default=None, max_length=32
    )
    manager_prompt_append: str | None = Field(default=None, max_length=2000)
    # DISC-1 §22.7 ("discussion → implementation" master switch) + DISC-2 §22A.5
    # (LLM grey-zone intent classifier) feature flags — tail-appended (§3.1).
    # ``None`` leaves any existing value untouched (PATCH semantics); a missing
    # key in the persisted meta resolves to OFF at orchestrator read time
    # (legacy conversations untouched). A fresh tab seeds these ON via the
    # front-end's enable-discussion full-config PATCH (用户 2026-06-24 拍板).
    implementation_enabled: bool | None = Field(default=None)
    intent_classifier_enabled: bool | None = Field(default=None)
    # DISC-1 TODO-2 — user-tunable numeric/string knobs, tail-appended (§3.1).
    # ``None`` leaves any existing value untouched (PATCH semantics); a missing
    # key resolves to the conservative default at orchestrator read time. Ranges
    # mirror the resolver clamp bounds (defence-in-depth: DTO rejects out-of-range
    # early, the resolver clamps anything that still slips through).
    # Run-level implementation budget caps (§22.5).
    impl_max_total_file_edits: int | None = Field(default=None, ge=1, le=100000)
    impl_max_total_exec_calls: int | None = Field(default=None, ge=1, le=100000)
    impl_max_total_runtime_seconds: int | None = Field(
        default=None, ge=1, le=86400
    )
    impl_max_total_changed_files: int | None = Field(
        default=None, ge=1, le=100000
    )
    # Soft-stop tuning thresholds (§22A.4).
    soft_stop_similarity: float | None = Field(default=None, ge=0.50, le=0.99)
    soft_stop_min_rounds: int | None = Field(default=None, ge=1, le=50)
    soft_stop_consecutive_turns: int | None = Field(default=None, ge=1, le=10)
    # Intent classifier model + timeout (§22A.5; resolver already reads these).
    intent_classifier_model: str | None = Field(default=None, max_length=128)
    intent_classifier_timeout_ms: int | None = Field(
        default=None, ge=200, le=60000
    )
    # Feature-item extractor (planner) model + timeout (§22.4).
    implementation_planner_model: str | None = Field(
        default=None, max_length=128
    )
    implementation_planner_timeout_ms: int | None = Field(
        default=None, ge=500, le=120000
    )
    # DISC-1 三期-step5 + 完成判定 B — validator / verify-command knobs,
    # tail-appended (§3.1). ``None`` leaves any existing value untouched (PATCH
    # semantics); a missing key resolves to the conservative default at
    # orchestrator read time (validator OFF, timeouts at constant defaults).
    implementation_validator_enabled: bool | None = Field(default=None)
    implementation_validator_timeout_ms: int | None = Field(
        default=None, ge=500, le=120000
    )
    implementation_verify_command_timeout_ms: int | None = Field(
        default=None, ge=1000, le=600000
    )


class ImplementationItemBody(BaseModel):
    """One feature/work-item in an implementation plan (DISC-1 二期-step4, §22.9).

    Mirrors :class:`~qai.chat.application.use_cases.implementation_plan.FeatureItem`
    field-for-field (snake_case). On the GET / PATCH **response** every field is
    surfaced so the panel can render the full plan.

    On the PATCH **request** the user may only author/edit the labels +
    ``assigned_role`` + a pending↔skipped ``status`` flip + add/delete items;
    every other field is BACKEND TRUTH (§🔴 State-Truth-First) and is preserved
    server-side regardless of what the request carries (the use case re-merges
    by id). All fields are therefore optional with defaults so a request need
    only send what it changes.
    """

    id: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    acceptance_criteria: list[str] = Field(default_factory=list)
    suggested_role: str | None = Field(default=None, max_length=64)
    assigned_role: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=32)
    result_summary: str | None = Field(default=None, max_length=2000)
    depends_on: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    attempt_count: int = 0
    started_at: str | None = Field(default=None, max_length=64)
    finished_at: str | None = Field(default=None, max_length=64)
    last_error: str | None = Field(default=None, max_length=1000)
    # DISC-1 三期-step5 / 完成判定 B — per-item verification command (additive,
    # §3.1). User-editable on the PATCH request; surfaced on the response.
    verify_command: str | None = Field(default=None, max_length=500)


class ImplementationPlanBody(BaseModel):
    """``GET`` / ``PATCH /api/chat/conversations/{id}/implementation`` response.

    The run-level plan envelope mirroring
    :class:`~qai.chat.application.use_cases.implementation_plan.ImplementationPlan`.
    When a conversation has no plan (flag-OFF / pre-extraction) the GET handler
    returns a STABLE empty shell (``phase="none"``, ``items=[]``, ``version=1``)
    so the response shape is invariant.
    """

    version: int = 1
    phase: str = "none"
    run_id: str | None = None
    current_item: str | None = None
    items: list[ImplementationItemBody] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    last_error: str | None = None
    stopped_by_user: bool = False
    paused_at: str | None = None


class ImplementationPlanPatchBody(BaseModel):
    """``PATCH /api/chat/conversations/{id}/implementation`` request body.

    The full ordered ``items`` array the front-end submits — treated as the
    authoritative item SET by id (existing id → merge user-editable fields;
    new id → append a fresh pending item; existing id absent → delete). Backend
    truth fields are preserved server-side; the run-state machine retains
    exclusive ownership of execution bookkeeping.
    """

    items: list[ImplementationItemBody] = Field(default_factory=list)


# ---- Helpers --------------------------------------------------------------


def _discussion_to_plan_body(
    discussion: dict[str, Any] | None,
) -> ImplementationPlanBody:
    """Shape a ``meta["discussion"]`` blob into the plan response body.

    Reads the plan via :func:`read_implementation_plan` + serialises through
    :func:`plan_to_dict` so the wire shape stays in lock-step with the domain
    model. A ``None`` plan (no discussion / flag-OFF / pre-extraction) yields a
    STABLE empty shell (``phase="none"``, ``items=[]``, ``version=1``) so the
    response shape is invariant regardless of plan presence.
    """
    plan = read_implementation_plan(discussion)
    if plan is None:
        return ImplementationPlanBody()
    raw = plan_to_dict(plan)
    return ImplementationPlanBody(
        version=raw["version"],
        phase=raw["phase"],
        run_id=raw["run_id"],
        current_item=raw["current_item"],
        items=[ImplementationItemBody(**item) for item in raw["items"]],
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        last_error=raw["last_error"],
        stopped_by_user=raw["stopped_by_user"],
        paused_at=raw["paused_at"],
    )


def _message_to_item(msg: Any) -> SubAgentMessageItem:
    """Project one structured ``Message`` → the front-end ``SubAgentMessageItem``.

    Full-unification forward serialisation (replaces the old ``_wire_to_messages``
    reverse-fold): the sub-agent's AUTHORITATIVE structured transcript
    (``SubAgentSession.messages``, built once at persist time by the shared
    ``wire_to_structured_messages``) is shaped EXACTLY like the main-agent
    ``GET …/messages`` rows — the tool-call cards already carry their executed
    ``output`` / ``status`` / ``durationMs`` / ``thought_signature`` — so the
    front-end reuses its STANDARD ``mapHistoryItems`` with zero divergence. A
    tool-call-only assistant turn keeps the ``[tool_calls]`` sentinel (the mapper
    normalises it to "").
    """
    tool_calls = [dict(c) for c in msg.tool_calls] if msg.tool_calls else None
    return SubAgentMessageItem(
        role=msg.role.value,
        text=msg.content.text,
        tool_calls=tool_calls,
        created_at=msg.created_at.isoformat(),
        usage=dict(msg.usage) if isinstance(msg.usage, dict) else None,
        meta=dict(msg.meta) if isinstance(msg.meta, dict) else None,
    )


def _subagent_messages(session: Any) -> list[SubAgentMessageItem]:
    """Return the sub-agent's renderable transcript items.

    SUBAGENT-UNIFY-6: the AUTHORITATIVE structured transcript
    (``session.messages``) is the SOLE truth source — every sub-agent run
    (autonomous + resume) and every user take-over persists it (the wire
    reverse-fold path + the ``wire_messages_json`` column it read are gone).
    ``system`` turns are dropped here (internal prompt, not shown to the user)
    — the structured transcript keeps them for resume rebuild, but the detail
    view hides them.
    """
    msgs = getattr(session, "messages", None) or []
    return [
        _message_to_item(m)
        for m in msgs
        if getattr(getattr(m, "role", None), "value", None) != "system"
    ]


def _subagent_to_summary(session) -> SubAgentSummary:  # type: ignore[no-untyped-def]
    """Map :class:`SubAgentSession` to its list-view wire DTO.

    Historical note: an earlier ``parent_conversation_id`` alias was carried
    alongside ``root_conversation_id`` in stage α; it was removed in β
    (front-end fully migrated to ``root_conversation_id``).
    """
    root_conv = session.root_conversation_id.value
    parent_sub = getattr(session, "parent_subagent_id", None)
    return SubAgentSummary(
        subagent_id=session.id.value,
        parent_message_id=(
            session.parent_message_id.value
            if session.parent_message_id is not None
            else None
        ),
        subagent_type=session.subagent_type,
        title=session.title,
        prompt_preview=session.prompt_preview,
        status=session.status.value,
        owner=session.owner.value,
        rounds=session.rounds,
        created_at=session.created_at.isoformat(),
        updated_at=session.updated_at.isoformat(),
        model_id=getattr(session, "model_id", None),
        model_provider=getattr(session, "model_provider", None),
        # Alpha unified-spawn-path tree edges (migration 049).
        root_conversation_id=root_conv,
        parent_subagent_id=(parent_sub.value if parent_sub is not None else None),
        depth=int(getattr(session, "depth", 1)),
    )


def _build_subagent_detail_response(  # type: ignore[no-untyped-def]
    session,
    usage,
    budget: int,
) -> SubAgentDetailResponse:
    """Compose a :class:`SubAgentDetailResponse` from a resolved session.

    Shared by the GET and PATCH detail routes so both surface identical fields
    — the alpha unified-spawn-path added tree-edge fields (migration 049) had
    to land in TWO builders otherwise; one helper keeps them in lock step
    (§3.6 cohesion). Historical note: an earlier ``parent_conversation_id``
    alias was carried alongside ``root_conversation_id`` in stage α; it was
    removed in β (front-end fully migrated to ``root_conversation_id``).
    """
    root_conv = session.root_conversation_id.value
    parent_sub = getattr(session, "parent_subagent_id", None)
    return SubAgentDetailResponse(
        subagent_id=session.id.value,
        status=session.status.value,
        owner=session.owner.value,
        subagent_type=session.subagent_type,
        title=session.title,
        prompt_preview=session.prompt_preview,
        rounds=session.rounds,
        created_at=session.created_at.isoformat(),
        updated_at=session.updated_at.isoformat(),
        messages=_subagent_messages(session),
        used_tokens=usage.used_clamped,
        budget_tokens=budget,
        ratio=usage.ratio,
        raw_used_tokens=usage.raw_used,
        raw_ratio=usage.raw_ratio,
        # Sub-agent回看 parity (migration 035): cumulative run usage +
        # per-round prompt snapshots (round-no keys coerced to str for JSON).
        token_usage=dict(session.usage) if session.usage is not None else None,
        round_snapshots=(
            {str(k): v for k, v in session.round_snapshots.items()}
            if session.round_snapshots is not None
            else None
        ),
        # Sub-agent spawn-permission (migration 045): surface the grant so
        # a take-over tab can default its "allow this sub-agent to spawn"
        # toggle ON when the main agent authorised it.
        allow_spawn=bool(getattr(session, "allow_spawn", False)),
        # Sub-agent model真值源 (migration 046): the sub-agent's OWN model id
        # + provider — the denominator used for ``budget_tokens`` above.
        model_id=getattr(session, "model_id", None),
        model_provider=getattr(session, "model_provider", None),
        # Alpha unified-spawn-path tree edges (migration 049).
        root_conversation_id=root_conv,
        parent_subagent_id=(parent_sub.value if parent_sub is not None else None),
        depth=int(getattr(session, "depth", 1)),
    )



def _participant_to_item(participant) -> ParticipantItem:  # type: ignore[no-untyped-def]
    """Map a :class:`Participant` aggregate to its discussion-roster wire DTO."""
    return ParticipantItem(
        id=participant.id.value,
        conversation_id=participant.conversation_id.value,
        kind=participant.kind.value,
        display_name=participant.display_name,
        model_id=participant.model_id,
        persona=participant.persona,
        config=dict(participant.config) if participant.config else None,
        created_at=participant.created_at.isoformat(),
        updated_at=participant.updated_at.isoformat(),
    )



def _conversation_to_summary(item) -> ConversationSummary:  # type: ignore[no-untyped-def]
    """Map :class:`ConversationListItem` to wire-format DTO."""
    c = item.conversation
    return ConversationSummary(
        id=c.id.value,
        title=c.title,
        status=c.status.value,
        created_at=c.created_at.isoformat(),
        updated_at=c.updated_at.isoformat(),
        message_count=item.message_count,
        round_count=item.round_count,
        tool_call_count=item.tool_call_count,
        subagent_count=getattr(item, "subagent_count", 0),
        meta=c.meta,
        pinned=c.pinned,
        favorite=c.favorite,
        # Present on the single-GET (full aggregate); the list projection loads
        # a 6-col header without detected_model_json, so this is None there
        # (the sidebar does not need it — only the promote CTA reads it).
        detected_model=getattr(c, "detected_model", None),
    )


# ---- Router factory -------------------------------------------------------


def build_router(*, container: "Container") -> APIRouter:
    """Build the chat REST router bound to ``container``."""
    router = APIRouter(prefix="/api/chat", tags=["chat"])

    async def _resolve_context_window(
        model_id: str | None, provider: str | None = None
    ) -> int:
        """Resolve a model's real context-window size (tokens).

        Truth-source order (State-Truth-First 铁律 1 — prefer the real
        per-model value over a hardcoded family guess):

        1. the cloud-models catalog's configured ``context_length`` for
           ``model_id`` (the authoritative value the model dropdown also
           shows — e.g. ``qwen3.7-max`` → 200000);
        2. otherwise the chat-domain family map
           :func:`get_context_limit` (covers local / unregistered models
           and supplies the 200K ``__unknown__`` fallback).

        ``provider`` disambiguates identical ``model_id``s living under
        different providers (e.g. ``claude-4-6-sonnet`` exposed by both
        ``provider_a`` at 128K and ``cloud LLM service`` at 200K). When supplied, only
        the matching provider's catalog entry is considered; when absent, the
        first catalog entry with that ``model_id`` is used (legacy behaviour).

        The catalog lives in the ``model_catalog`` context; this route
        (interfaces layer) is allowed to read it directly — cross-context
        coordination at the composition edge, not a domain import.
        """
        if not model_id:
            return get_context_limit(model_id or "")
        try:
            entries = (
                await container.model_catalog.list_cloud_models_use_case.execute()
            )
        except Exception:  # pragma: no cover - catalog read is best-effort
            entries = []
        want_provider = (provider or "").strip()
        fallback_len: int | None = None
        for entry in entries:
            if entry.get("model_id") != model_id:
                continue
            ctx_len = entry.get("context_length")
            usable = isinstance(ctx_len, int) and ctx_len > 0
            # Exact provider match wins immediately.
            if want_provider and entry.get("provider") == want_provider:
                if usable:
                    return ctx_len
                fallback_len = None
                break
            # No provider filter (or provider not yet matched): remember the
            # first usable entry so an unfiltered/legacy call still resolves.
            if not want_provider and usable and fallback_len is None:
                fallback_len = ctx_len
        if fallback_len is not None:
            return fallback_len
        # Not in the cloud catalog (or no usable context_length) → fall back
        # to the chat-domain family map.
        return get_context_limit(model_id)

    @router.post(
        "/conversations",
        response_model=ConversationSummary,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_conversation(
        body: CreateConversationRequest,
    ) -> ConversationSummary:
        conv = await container.chat.create_conversation_use_case.execute(
            CreateConversationInput(title=body.title),
        )
        return ConversationSummary(
            id=conv.id.value,
            title=conv.title,
            status=conv.status.value,
            created_at=conv.created_at.isoformat(),
            updated_at=conv.updated_at.isoformat(),
            message_count=conv.message_count,
            meta=conv.meta,
            pinned=conv.pinned,
            favorite=conv.favorite,
        )

    @router.get(
        "/conversations",
        response_model=ConversationListResponse,
    )
    async def list_conversations(
        query: str | None = Query(default=None, max_length=256),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        favorite: bool = Query(default=False),
        pinned: bool = Query(default=False),
    ) -> ConversationListResponse:
        items = await container.chat.list_conversations_use_case.execute(
            ListConversationsInput(
                query=query,
                limit=limit,
                offset=offset,
                favorite_only=favorite,
                pinned_only=pinned,
            ),
        )
        return ConversationListResponse(
            items=[_conversation_to_summary(i) for i in items],
        )

    @router.get(
        "/conversations/{conversation_id}",
        response_model=ConversationSummary,
    )
    async def get_conversation(
        conversation_id: str,
    ) -> ConversationSummary:
        # No GetConversationUseCase exists; fetch via repo port directly
        # (read-path composition, not a write — see _chat_di.py docstring).
        conv = await container.chat.conversations.get(
            ConversationId.of(conversation_id),
        )
        return ConversationSummary(
            id=conv.id.value,
            title=conv.title,
            status=conv.status.value,
            created_at=conv.created_at.isoformat(),
            updated_at=conv.updated_at.isoformat(),
            message_count=conv.message_count,
            meta=conv.meta,
            pinned=conv.pinned,
            favorite=conv.favorite,
            # Promote-ready detection (migration 057): the frontend CTA reads
            # this from the single-GET so opening a conversation needs 0 disk
            # scans. None until the first turn-end detection persists it.
            detected_model=getattr(conv, "detected_model", None),
        )

    @router.get(
        "/conversations/{conversation_id}/messages",
        response_model=MessagesPageResponse,
    )
    async def get_messages(
        conversation_id: str,
        cursor: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> MessagesPageResponse:
        page = await container.chat.get_conversation_messages_use_case.execute(
            GetConversationMessagesInput(
                conversation_id=ConversationId.of(conversation_id),
                cursor=cursor,
                limit=limit,
            ),
        )
        items = [
            MessageItem(
                id=m.id.value,
                role=m.role.value,
                text=m.content.text,
                created_at=m.created_at.isoformat(),
                parent_id=m.parent_id.value if m.parent_id else None,
                tool_calls=list(m.tool_calls) if m.tool_calls else None,
                usage=dict(m.usage) if m.usage else None,
                model_id=m.model_id,
                model_provider=m.model_provider,
                meta=dict(m.meta) if m.meta else None,
                sender_id=m.sender_id,
            )
            for m in page.items
        ]
        return MessagesPageResponse(items=items, next_cursor=page.next_cursor)

    @router.patch(
        "/conversations/{conversation_id}",
        response_model=ConversationSummary,
    )
    async def rename_conversation(
        conversation_id: str,
        body: RenameConversationRequest,
    ) -> ConversationSummary:
        conv = await container.chat.rename_conversation_use_case.execute(
            RenameConversationInput(
                conversation_id=ConversationId.of(conversation_id),
                new_title=body.title,
            ),
        )
        return ConversationSummary(
            id=conv.id.value,
            title=conv.title,
            status=conv.status.value,
            created_at=conv.created_at.isoformat(),
            updated_at=conv.updated_at.isoformat(),
            message_count=conv.message_count,
            meta=conv.meta,
            pinned=conv.pinned,
            favorite=conv.favorite,
        )

    @router.patch(
        "/conversations/{conversation_id}/workspace",
        response_model=ConversationSummary,
    )
    async def set_conversation_workspace(
        conversation_id: str,
        body: SetConversationWorkspaceRequest,
    ) -> ConversationSummary:
        conv = await container.chat.set_conversation_workspace_use_case.execute(
            SetConversationWorkspaceInput(
                conversation_id=ConversationId.of(conversation_id),
                workspace=body.workspace,
            ),
        )
        # SEC true-scoping (PART E) — sync this conversation's SESSION-scoped
        # workspace PathGrant with the new workspace. On a non-empty
        # workspace the bridge auto-creates a session read+write grant for
        # that path (so the AI can work there without re-prompting, only for
        # this conversation); on a CHANGED or CLEARED workspace it first
        # revokes this conversation's prior AUTO session-workspace grant
        # (SEC-WORKSPACE-GRANT-REVOKE-1) so stale directory access does not
        # linger. Called unconditionally (incl. clear) so revoke-on-clear
        # runs. The wiring lives in the apps/api layer bridge because
        # ``qai.chat`` must not import ``qai.security`` (context-isolation);
        # the route only names the apps-layer seam + the container.
        # Best-effort / non-raising: a grant hiccup never fails set-workspace.
        from apps.api._workspace_grant_bridge import (
            ensure_workspace_session_grant,
        )

        await ensure_workspace_session_grant(
            container,
            conversation_id=conversation_id,
            workspace=body.workspace or "",
        )
        return ConversationSummary(
            id=conv.id.value,
            title=conv.title,
            status=conv.status.value,
            created_at=conv.created_at.isoformat(),
            updated_at=conv.updated_at.isoformat(),
            message_count=conv.message_count,
            meta=conv.meta,
            pinned=conv.pinned,
            favorite=conv.favorite,
        )

    @router.patch(
        "/conversations/{conversation_id}/pin",
        response_model=ConversationSummary,
    )
    async def set_conversation_pinned(
        conversation_id: str,
        body: SetConversationPinnedRequest,
    ) -> ConversationSummary:
        conv = await container.chat.set_conversation_pinned_use_case.execute(
            SetConversationPinnedInput(
                conversation_id=ConversationId.of(conversation_id),
                pinned=body.pinned,
            ),
        )
        return ConversationSummary(
            id=conv.id.value,
            title=conv.title,
            status=conv.status.value,
            created_at=conv.created_at.isoformat(),
            updated_at=conv.updated_at.isoformat(),
            message_count=conv.message_count,
            meta=conv.meta,
            pinned=conv.pinned,
            favorite=conv.favorite,
        )

    @router.patch(
        "/conversations/{conversation_id}/favorite",
        response_model=ConversationSummary,
    )
    async def set_conversation_favorite(
        conversation_id: str,
        body: SetConversationFavoriteRequest,
    ) -> ConversationSummary:
        conv = await container.chat.set_conversation_favorite_use_case.execute(
            SetConversationFavoriteInput(
                conversation_id=ConversationId.of(conversation_id),
                favorite=body.favorite,
            ),
        )
        return ConversationSummary(
            id=conv.id.value,
            title=conv.title,
            status=conv.status.value,
            created_at=conv.created_at.isoformat(),
            updated_at=conv.updated_at.isoformat(),
            message_count=conv.message_count,
            meta=conv.meta,
            pinned=conv.pinned,
            favorite=conv.favorite,
        )

    @router.patch(
        "/conversations/{conversation_id}/budget",
        response_model=ConversationBudgetResponse,
    )
    async def set_conversation_budget(
        conversation_id: str,
        body: SetConversationBudgetRequest,
    ) -> ConversationBudgetResponse:
        # Configure the per-conversation cap through the application PORT
        # (``ChatServices.budget_tracker`` — a ``BudgetTrackerPort``); the route
        # never imports ``qai.chat.adapters`` (interfaces→adapters is forbidden
        # by import-linter). The container always exposes a persistence-backed
        # tracker here (the ``chat_budget_enabled`` gate only governs whether the
        # streaming loop ENFORCES the cap, not whether it can be configured /
        # read). ``set_max_tokens`` raises ``ConversationNotFoundError`` (→ 404)
        # when the conversation is missing. ``max_tokens=None`` disables it;
        # ``reset_used`` zeroes the running counter.
        tracker = container.chat.budget_tracker
        conv_id = ConversationId.of(conversation_id)
        result = await tracker.set_max_tokens(conv_id, body.max_tokens)
        if body.reset_used:
            await tracker.reset(conv_id)
            result = await tracker.check(conv_id)
        return ConversationBudgetResponse(
            max_tokens=result.max_tokens,
            used_tokens=result.used,
            exceeded=result.exceeded,
            remaining=result.remaining,
            enabled=result.enabled,
        )

    @router.delete(
        "/conversations/{conversation_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model=None,
    )
    async def delete_conversation(conversation_id: str) -> None:
        await container.chat.delete_conversation_use_case.execute(
            DeleteConversationInput(
                conversation_id=ConversationId.of(conversation_id),
            ),
        )
        return None

    @router.post(
        "/conversations/{conversation_id}/compact",
        response_model=ContextSizeResponse,
    )
    async def compact_chat(
        conversation_id: str,
        body: CompactChatRequest,
    ) -> ContextSizeResponse:
        result = await container.chat.compact_chat_use_case.execute(
            CompactChatInput(
                conversation_id=ConversationId.of(conversation_id),
                budget_tokens=body.budget_tokens,
                trigger_threshold=body.trigger_threshold,
            ),
        )
        return ContextSizeResponse(
            used_tokens=result.context_size.used.value,
            budget_tokens=result.context_size.budget.value,
            ratio=result.context_size.ratio,
            needs_compaction=result.needs_compaction,
            raw_used_tokens=result.raw_used_tokens,
            raw_ratio=result.raw_ratio,
        )

    @router.get(
        "/conversations/{conversation_id}/context",
        response_model=ContextSizeResponse,
    )
    async def get_context_size(
        conversation_id: str,
        budget_tokens: int = Query(default=8192, gt=0, le=10_000_000),
        trigger_threshold: float = Query(default=0.8, ge=0.0, le=1.0),
        model_id: str | None = Query(default=None, max_length=256),
        provider: str | None = Query(default=None, max_length=128),
    ) -> ContextSizeResponse:
        # Reuse CompactChatUseCase for the decision; query only.
        # The budget is the model's REAL context window: when ``model_id``
        # is supplied we resolve it against the cloud-models catalog first
        # (authoritative — same value the dropdown shows, e.g. 200K for
        # qwen3.7-max), falling back to the chat-domain family map for
        # local / unregistered models. ``provider`` disambiguates identical
        # model_ids under different providers (e.g. claude-4-6-sonnet 128K via
        # provider_a vs 200K via cloud LLM service). We pass the resolved window as
        # ``budget_tokens`` and clear ``model_id`` so the use case honours
        # it verbatim instead of re-deriving the (fuzzy) family limit.
        # Without ``model_id`` the caller-supplied ``budget_tokens`` default
        # (8192) is honoured — fully backwards-compatible.
        if model_id:
            budget_tokens = await _resolve_context_window(model_id, provider)
        result = await container.chat.compact_chat_use_case.execute(
            CompactChatInput(
                conversation_id=ConversationId.of(conversation_id),
                budget_tokens=budget_tokens,
                trigger_threshold=trigger_threshold,
                model_id=None,
            ),
        )
        # Compression-state badge (post-compaction figure). The compact use
        # case and the stream use case are two singletons in the SAME
        # ``container.chat``, so the stream use case's live checkpoint probe is
        # comparable口径 with ``used_tokens``: both prefer the provider-measured
        # ``usage`` (``used_tokens`` = the per-conversation full-history running
        # counter; ``compacted_tokens`` = the last round's measured wire), and
        # only fall back to a coarse char estimate when no cloud usage exists.
        # State-Truth-First (铁律 1): the
        # figure is computed live, never cached. Read-path composition fetches
        # the conversation via the repo port (same pattern as get_conversation).
        conv = await container.chat.conversations.get(
            ConversationId.of(conversation_id),
        )
        # CCD-5 (State-Truth-First, AGENTS.md 铁律 1): this badge probe runs
        # OUTSIDE a chat turn, so the stream use case's in-memory compaction-
        # checkpoint cache may be cold (fresh process / reopened tab) even when
        # a checkpoint is durably persisted in sqlite. Restore it from the
        # durable store FIRST so the sync ``estimate_compacted_tokens`` below
        # reports the REAL persisted state (``has_checkpoint=True``) instead of
        # a cold-cache illusion that makes the UI claim "uncompressed" / look
        # like it will recompress. No-op when no durable store is wired.
        await container.chat.stream_chat_use_case.ensure_compaction_checkpoint_loaded(
            conv,
        )
        # Second CPU-bound pass of this same request (system prompt rebuild +
        # full-history estimate). Like the estimate above, run it OFF the
        # event loop so a long history can't stall other requests. Same inputs,
        # same number — purely a threading change.
        compacted_tokens = await asyncio.to_thread(
            container.chat.stream_chat_use_case.estimate_compacted_tokens,
            conv,
            model_id,
        )
        # DIAG (token-display investigation): the EXACT badge payload the
        # frontend renders. ``used_tokens`` is the occupancy (left "~216.8K");
        # ``compacted_tokens`` is the "发给模型 ~7.1K / 省%" figure. When
        # compacted_tokens collapses far below used_tokens on a long history,
        # this surfaces both numbers + the model window so we can see the ratio
        # the UI computes. Remove once root-caused.
        _log.info(
            "chat.diag.context_badge",
            conversation_id=conversation_id,
            model_id=model_id,
            used_tokens=result.context_size.used.value,
            raw_used_tokens=result.raw_used_tokens,
            budget_tokens=result.context_size.budget.value,
            compacted_tokens=compacted_tokens,
            compacted=compacted_tokens is not None,
            needs_compaction=result.needs_compaction,
            message_count=len(getattr(conv, "messages", ()) or ()),
        )
        return ContextSizeResponse(
            used_tokens=result.context_size.used.value,
            budget_tokens=result.context_size.budget.value,
            ratio=result.context_size.ratio,
            needs_compaction=result.needs_compaction,
            compacted_tokens=compacted_tokens,
            compacted=compacted_tokens is not None,
            raw_used_tokens=result.raw_used_tokens,
            raw_ratio=result.raw_ratio,
        )

    @router.post(
        "/stop",
        response_model=StopChatResponse,
    )
    async def stop_chat(body: StopChatRequest) -> StopChatResponse:
        # Idempotent — stopping an already-finished or unknown stream
        # is NOT an error (front-end may race the natural end-of-stream).
        result = await container.chat.stop_chat_use_case.execute(
            StopChatInput(
                tab_id=TabId.of(body.tab_id),
                reason=body.reason,
            ),
        )
        if result.aborted:
            container.chat.chat_stream_broadcaster.mark_aborted(
                TabId.of(body.tab_id),
                reason=body.reason,
            )
        return StopChatResponse(aborted=result.aborted, reason=result.reason)

    @router.post(
        "/cancel_tool",
        response_model=CancelToolResponse,
    )
    async def cancel_tool(body: CancelToolRequest) -> CancelToolResponse:
        # Idempotent — cancelling an already-finished / unknown tool call is
        # NOT an error (the front-end may race the tool's natural completion).
        # Unlike /stop this does NOT abort the turn: the backend marks just
        # this one call for cancellation, synthesizes a [cancelled] result, and
        # the turn continues with the other tools' results.
        result = await container.chat.cancel_tool_use_case.execute(
            CancelToolInput(
                tab_id=TabId.of(body.tab_id),
                call_id=body.call_id,
            ),
        )
        return CancelToolResponse(
            cancelled=result.cancelled, call_id=result.call_id,
        )

    @router.post(
        "/answer",
        response_model=AnswerQuestionResponse,
    )
    async def answer_question(
        body: AnswerQuestionRequest,
    ) -> AnswerQuestionResponse:
        # Idempotent — answering a question that already timed out / was
        # cancelled / never existed is NOT an error (the front-end may race
        # the question's hard timeout).  Resolving the future wakes the
        # suspended ``question`` tool handler so the agentic loop continues.
        delivered = container.chat.question_registry.resolve(
            TabId.of(body.tab_id),
            body.answer,
        )
        return AnswerQuestionResponse(delivered=delivered)

    # ---- Tool catalogue (2026-06-21) -------------------------------------

    @router.get(
        "/tools",
        response_model=ListChatToolsResponse,
    )
    async def list_chat_tools() -> ListChatToolsResponse:
        """List all currently registered chat tools.

        The list reflects the LIVE :class:`ToolInvocationPort` registry, so a
        newly-registered tool appears automatically — the front-end (Discussion
        role allowlist UI) iterates over this list rather than hard-coding the
        tool set. Ordered by the chat adapter's canonical ``TOOL_ORDER``.

        ``available_in_discussion`` reads
        :data:`qai.chat.application.use_cases.orchestrate_discussion._DISCUSSION_EXCLUDED_TOOLS`
        (the single source of truth) so a future hard-block in the back-end
        propagates here without touching this route. Currently the set is
        empty by user mandate (2026-06-21): every tool — including ``agent``
        and ``question`` — is selectable per-role in the Discussion panel,
        with sub-agent recursion guarded INSIDE the ``agent`` tool handler.

        The ``agent`` tool schema is appended dynamically here because the
        registry's ``schemas()`` reflects only the always-registered tools;
        the ``agent`` schema is round-injected by ``streaming._collect_tools_schemas``
        in main-loop turns and lives as a module-level helper. Exposing it
        here keeps the catalogue useful for the Discussion allowlist UI
        (judgement 1: single source of truth = ``_agent_tool_schema`` from
        the same module that defines it for the main loop).
        """
        # Local imports keep the module-level import surface lean and avoid a
        # cycle with the use-case package at import time.
        from qai.chat.application.use_cases.orchestrate_discussion import (
            _DISCUSSION_EXCLUDED_TOOLS,
        )
        from qai.chat.application.use_cases.streaming import (
            _agent_tool_schema,
        )

        # Mode-conditional tools (mirrors the constant in
        # ``streaming.py:_CONDITIONAL_TOOL_NAMES`` — kept private there, so we
        # reproduce the small frozenset here rather than promoting it). When
        # the set drifts in either direction, this route degrades to "marked
        # not-conditional" which is the safer of the two failure modes.
        _conditional_tools = frozenset(
            {"appbuilder_run", "appbuilder_batch_run"}
        )

        tools_port = container.chat.tools
        schemas_fn = getattr(tools_port, "schemas", None)
        advertised: list[dict[str, Any]] = []
        if callable(schemas_fn):
            try:
                advertised = list(schemas_fn())
            except Exception:  # noqa: BLE001 — degrade to empty list
                advertised = []

        # Append the ``agent`` schema unless it is already present (some test
        # builds may register it directly into the registry). Position is
        # cosmetic only — the front-end re-orders by display rules.
        if not any(
            isinstance(s, dict)
            and isinstance(s.get("function"), dict)
            and s["function"].get("name") == "agent"
            for s in advertised
        ):
            try:
                advertised.append(_agent_tool_schema())
            except Exception:  # noqa: BLE001 — defensive
                pass

        descriptors: list[ChatToolDescriptor] = []
        for schema in advertised:
            if not isinstance(schema, dict):
                continue
            fn = schema.get("function")
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            if not isinstance(name, str) or not name:
                continue
            descriptors.append(
                ChatToolDescriptor(
                    name=name,
                    description=str(fn.get("description") or ""),
                    available_in_discussion=(
                        name not in _DISCUSSION_EXCLUDED_TOOLS
                    ),
                    conditional=(name in _conditional_tools),
                )
            )
        return ListChatToolsResponse(tools=descriptors)

    # ---- Experience CRUD (PR-042 / issue d) ------------------------------

    @router.post(
        "/experiences",
        response_model=ExperienceItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def save_experience(body: SaveExperienceRequest) -> ExperienceItem:
        exp = await container.chat.save_experience_use_case.execute(
            SaveExperienceInput(
                category=body.category,
                content=body.content,
                metadata=body.metadata,
                experience_id=body.experience_id,
            ),
        )
        return ExperienceItem(
            id=exp.id.value,
            category=exp.category,
            content=exp.content,
            metadata=dict(exp.metadata),
            created_at=exp.created_at.isoformat(),
        )

    @router.get(
        "/experiences",
        response_model=ExperienceListResponse,
    )
    async def list_experiences(
        category: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> ExperienceListResponse:
        items = await container.chat.list_experiences_use_case.execute(
            ListExperiencesInput(category=category, limit=limit),
        )
        return ExperienceListResponse(
            items=[
                ExperienceItem(
                    id=e.id.value,
                    category=e.category,
                    content=e.content,
                    metadata=dict(e.metadata),
                    created_at=e.created_at.isoformat(),
                )
                for e in items
            ],
        )

    @router.get(
        "/experiences/categories",
        response_model=ExperienceCategoriesResponse,
    )
    async def list_experience_categories() -> ExperienceCategoriesResponse:
        cats = await container.chat.list_experience_categories_use_case.execute()
        return ExperienceCategoriesResponse(categories=list(cats))

    @router.delete(
        "/experiences/{experience_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model=None,
    )
    async def delete_experience(experience_id: str) -> None:
        await container.chat.delete_experience_use_case.execute(
            DeleteExperienceInput(experience_id=experience_id),
        )
        return None

    # ---- Tab management (PR-043 / issue d) -------------------------------

    @router.post(
        "/tabs",
        response_model=TabItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def open_tab(body: OpenTabRequest) -> TabItem:
        tab = await container.chat.open_tab_use_case.execute(
            OpenTabInput(
                conversation_id=body.conversation_id,
                tab_id=body.tab_id,
            ),
        )
        return TabItem(
            id=tab.id.value,
            conversation_id=tab.conversation_id.value,
            status=tab.status.value,
            created_at=tab.created_at.isoformat(),
            last_active_at=tab.last_active_at.isoformat(),
        )

    @router.get(
        "/tabs",
        response_model=TabListResponse,
    )
    async def list_active_tabs() -> TabListResponse:
        tabs = await container.chat.list_active_tabs_use_case.execute()
        return TabListResponse(
            items=[
                TabItem(
                    id=t.id.value,
                    conversation_id=t.conversation_id.value,
                    status=t.status.value,
                    created_at=t.created_at.isoformat(),
                    last_active_at=t.last_active_at.isoformat(),
                )
                for t in tabs
            ],
        )

    @router.get(
        "/active-runs",
        response_model=ActiveRunsResponse,
    )
    async def list_active_runs() -> ActiveRunsResponse:
        """List process-local ordinary chat and sub-agent runs."""
        items: list[ActiveRunItem] = []

        abort_by_tab = {
            snap.tab_id.value: snap
            for snap in container.chat.abort_registry.list_active()
        }
        seen_chat_tabs: set[str] = set()
        for run in container.chat.chat_stream_broadcaster.list_active():
            tab_id = run.tab_id.value
            seen_chat_tabs.add(tab_id)
            abort = abort_by_tab.get(tab_id)
            conversation_id = (
                run.conversation_id.value if run.conversation_id is not None else None
            )
            title = run.title
            status_value: str | None = "streaming"
            last_active_at = run.last_active_at
            if title is None and run.conversation_id is not None:
                conv = await container.chat.conversations.find(run.conversation_id)
                if conv is not None:
                    title = conv.title
            tab = await container.chat.tabs.find(run.tab_id)
            if tab is not None:
                status_value = tab.status.value
                last_active_at = tab.last_active_at
                if conversation_id is None:
                    conversation_id = tab.conversation_id.value
            aborted = run.aborted or bool(abort.aborted if abort is not None else False)
            reason = run.reason or (abort.reason if abort is not None else None)
            items.append(
                ActiveRunItem(
                    kind="chat",
                    id=tab_id,
                    tab_id=tab_id,
                    conversation_id=conversation_id,
                    title=title,
                    status="aborting" if aborted else status_value,
                    model_id=run.model_id,
                    model_provider=run.model_provider,
                    started_at=run.started_at.isoformat(),
                    last_active_at=last_active_at.isoformat(),
                    aborted=aborted,
                    reason=reason,
                    openable=conversation_id is not None,
                    attach_path=f"/api/chat/active-runs/{tab_id}/ws",
                    stop=ActiveRunStopSpec(
                        path="/api/chat/stop",
                        body={"tab_id": tab_id, "reason": "user_requested"},
                    ),
                ),
            )

        for tab_id, abort in abort_by_tab.items():
            if tab_id in seen_chat_tabs:
                continue
            tab = await container.chat.tabs.find(abort.tab_id)
            conversation_id = tab.conversation_id.value if tab is not None else None
            title: str | None = None
            if tab is not None:
                conv = await container.chat.conversations.find(tab.conversation_id)
                if conv is not None:
                    title = conv.title
            items.append(
                ActiveRunItem(
                    kind="chat",
                    id=tab_id,
                    tab_id=tab_id,
                    conversation_id=conversation_id,
                    title=title,
                    status="aborting" if abort.aborted else "streaming",
                    started_at=abort.started_at.isoformat(),
                    last_active_at=(
                        tab.last_active_at.isoformat()
                        if tab is not None
                        else abort.started_at.isoformat()
                    ),
                    aborted=abort.aborted,
                    reason=abort.reason,
                    openable=conversation_id is not None,
                    attach_path=(
                        f"/api/chat/active-runs/{tab_id}/ws"
                        if container.chat.chat_stream_broadcaster.get(abort.tab_id)
                        is not None
                        else None
                    ),
                    stop=ActiveRunStopSpec(
                        path="/api/chat/stop",
                        body={"tab_id": tab_id, "reason": "user_requested"},
                    ),
                ),
            )

        for snap in container.chat.subagent_abort_registry.list_active():
            session = await container.chat.subagent_sessions.find(
                SubAgentSessionId.of(snap.subagent_id),
            )
            if session is None:
                continue
            items.append(
                ActiveRunItem(
                    kind="subagent",
                    id=snap.subagent_id,
                    subagent_id=snap.subagent_id,
                    root_conversation_id=session.root_conversation_id.value,
                    title=session.title,
                    status="aborting" if snap.aborted else session.status.value,
                    started_at=snap.started_at.isoformat(),
                    last_active_at=session.updated_at.isoformat(),
                    aborted=snap.aborted,
                    reason=snap.reason,
                    openable=True,
                    attach_path=f"/api/chat/subagents/{snap.subagent_id}/ws",
                    stop=ActiveRunStopSpec(
                        path=f"/api/chat/subagents/{snap.subagent_id}/interrupt",
                        body={},
                    ),
                ),
            )

        return ActiveRunsResponse(
            items=sorted(items, key=lambda item: item.started_at),
        )

    @router.delete(
        "/tabs/{tab_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model=None,
    )
    async def close_tab(tab_id: str) -> None:
        await container.chat.close_tab_use_case.execute(
            CloseTabInput(tab_id=tab_id),
        )
        return None

    # ---- Sub-agent inspection / interrupt --------------------------------

    @router.get(
        "/conversations/{conversation_id}/subagents",
        response_model=SubAgentListResponse,
    )
    async def list_subagents(conversation_id: str) -> SubAgentListResponse:
        # Read-path composition via the repo port directly (no use case
        # exists; mirrors get_conversation above). Returns summaries only —
        # the structured messages transcript is materialised lazily by the
        # detail endpoint.
        sessions = await container.chat.subagent_sessions.list_by_root_conversation(
            ConversationId.of(conversation_id),
        )
        return SubAgentListResponse(
            items=[_subagent_to_summary(s) for s in sessions],
        )

    @router.get(
        "/subagents/{subagent_id}",
        response_model=SubAgentDetailResponse,
    )
    async def get_subagent(
        subagent_id: str,
        model_id: str | None = Query(default=None, max_length=256),
        provider: str | None = Query(default=None, max_length=128),
    ) -> SubAgentDetailResponse:
        # ``get`` raises SubAgentSessionNotFoundError on a miss; the global
        # NotFoundError handler (interfaces error middleware) maps it to a
        # 404 envelope — same path get_conversation relies on, so no local
        # try/except is needed here.
        session = await container.chat.subagent_sessions.get(
            SubAgentSessionId.of(subagent_id),
        )
        # Sub-agent's OWN context usage (badge fix): read the running
        # ``last_prompt_tokens`` counter (replace-last semantics, maintained per
        # round by ``SubAgentSession.accumulate_usage`` from the round's
        # ``last_round_prompt_tokens`` / ``prompt_tokens``). State-Truth-First
        # (AGENTS.md 铁律 1): that figure is the provider's measured wire size of
        # the most recent round — strictly more accurate than a local BPE
        # re-tokenisation of the persisted wire, and far cheaper (no off-loop
        # tiktoken pass). This is the truthful per-sub-agent figure — the
        # per-conversation /context endpoint would report the PARENT
        # conversation's usage because a standalone sub-agent tab carries the
        # parent's conversation id.
        #
        # BUDGET口径 (State-Truth-First 铁律 1 / 铁律 4, migration 046): the
        # budget is the SUB-AGENT's OWN model window, and the单一真值源 is now
        # the persisted ``session.model_id`` / ``session.model_provider``. The
        # query ``model_id`` / ``provider`` are kept ONLY as a fallback for a
        # legacy session that recorded no model (NULL columns) — older clients
        # still pass them, and a pre-046 row round-trips to ``None``. This
        # matches the LIVE frame's ``context_limit`` (resolved from the
        # sub-agent's persisted model in ``agent_tool.py``), so the cold-open GET
        # snapshot and the running LIVE refresh agree on the window.
        used = int(getattr(session, "last_prompt_tokens", None) or 0)
        _eff_model_id = getattr(session, "model_id", None) or model_id
        _eff_provider = getattr(session, "model_provider", None) or provider
        budget = await _resolve_context_window(_eff_model_id, _eff_provider)
        # Shared口径 with the main agent's badge via ``compute_context_usage``
        # (judgement 1: one calculation). ``used_tokens`` / ``ratio`` are
        # clamped to the window (floor 100%); ``raw_used_tokens`` / ``raw_ratio``
        # (tail-appended §3.1) preserve the over-window truth so the sub-agent
        # badge can show >100% at parity with the main agent.
        usage = compute_context_usage(used, budget)
        return _build_subagent_detail_response(session, usage, budget)

    @router.patch(
        "/subagents/{subagent_id}",
        response_model=SubAgentDetailResponse,
    )
    async def update_subagent_model(
        subagent_id: str,
        body: UpdateSubAgentModelRequest,
    ) -> SubAgentDetailResponse:
        """Set the sub-agent's OWN model (the budget-denominator真值源).

        State-Truth-First (铁律 1 / 铁律 4) + 拍板 design: a sub-agent persists
        its own model (migration 046); this endpoint is the single write path
        for the user switching THIS sub-agent's model in its standalone tab. It
        changes ONLY the budget denominator — the sub-agent's ``used`` /
        ``last_prompt_tokens`` (历史实测 numerator) is left untouched, so the
        ratio shifts purely because the window changed (NOT because used was
        re-derived from a model swap). Returns the refreshed detail with
        ``budget_tokens`` re-resolved from the new model and ``used_tokens``
        unchanged. CSRF is enforced by the global middleware (qai_csrf cookie /
        X-QAI-CSRF header) for all mutating methods — same as the sibling
        POST/PATCH/DELETE chat routes; no per-handler CSRF dependency exists.
        """
        # ``get`` raises SubAgentSessionNotFoundError on a miss → the global
        # NotFoundError handler maps it to a 404 envelope (same path get_subagent
        # relies on; new route, no existing contract changed — §3.1).
        session = await container.chat.subagent_sessions.get(
            SubAgentSessionId.of(subagent_id),
        )
        session.set_model(
            body.model_id,
            body.model_provider,
            now=container.clock.now(),
        )
        await container.chat.subagent_sessions.save(session)
        # Re-resolve the budget from the NEW model (the单一真值源); ``used`` comes
        # from the UNCHANGED ``last_prompt_tokens`` so only the denominator moves.
        used = int(getattr(session, "last_prompt_tokens", None) or 0)
        budget = await _resolve_context_window(
            session.model_id, session.model_provider
        )
        usage = compute_context_usage(used, budget)
        return _build_subagent_detail_response(session, usage, budget)

    @router.get(
        "/subagents/{subagent_id}/stream",
        responses={
            200: {
                "content": {"text/event-stream": {}},
                "description": "SSE stream of a running sub-agent's events.",
            }
        },
    )
    async def stream_subagent(
        subagent_id: str,
        from_seq: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        """Live SSE stream of a running sub-agent's events (block 2).

        Independent of the parent conversation's SSE stream: a standalone
        sub-agent tab opens this endpoint to (1) backfill the events it
        missed since the sub-agent started and (2) follow live frames until
        the sub-agent finishes. The replay state machine lives in
        :meth:`SubAgentStreamBroadcaster.replay` — it serves the in-memory
        frame buffer when an entry exists, else falls back to the persisted
        :class:`SubAgentSession` snapshot (server restarted / TTL expired).

        ``from_seq`` (default 0 — full replay, byte-parity with the historical
        contract; additive per §3.1) — a reconnecting client that already
        applied frames up to sequence *S* passes ``from_seq=S + 1`` so the
        broadcaster emits ONLY frames with ``sequence >= from_seq``. Mirrors
        the ``subagent_ws`` sibling and the main-agent ``active_run_ws``
        design so a WS-drop reconnect (or a tab-switch reuse) stitches onto
        the already-rendered transcript without duplication.

        Wire shape mirrors the App Builder ``/runs/{id}/stream`` endpoint:

        * ``event: frame\\ndata: {"sequence": int, "payload": {...}}`` — one
          per buffered sub-agent event (``payload.type`` is
          ``subagent_output`` / ``subagent_tool`` / ``subagent_done`` / ...).
        * ``event: state\\ndata: {...}`` / ``event: done\\ndata: {...}`` /
          ``event: error\\ndata: <envelope>`` on the snapshot-fallback path.

        Two concurrent subscribers (the main-agent run forwarding inline in
        the parent tab AND a user watching here) are independent fan-outs and
        do not interfere (requirement ③).
        """
        from ._sse import _sse_event

        sid = SubAgentSessionId.of(subagent_id)
        broadcaster = container.chat.subagent_stream_broadcaster
        repo = container.chat.subagent_sessions

        async def _generator() -> AsyncIterator[bytes]:
            async for event_name, payload in broadcaster.replay(
                sid, repository=repo, from_seq=from_seq,
            ):
                yield _sse_event(event_name, payload)

        return StreamingResponse(
            _generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post(
        "/subagents/{subagent_id}/interrupt",
        response_model=SubAgentInterruptResponse,
    )
    async def interrupt_subagent(
        subagent_id: str,
    ) -> SubAgentInterruptResponse:
        # Block 3 — a sub-agent now has its OWN cooperative-cancellation flag
        # (keyed by its id) registered by the sub-agent loop for the duration
        # of a run. Signalling it stops ONLY this sub-agent (after its current
        # round) WITHOUT touching the parent tab's stream / the main agent.
        #
        # State-Truth-First (AGENTS.md §7): we resolve the session (404 on a
        # miss, idempotently) and report the TRUTHFUL outcome — ``aborted`` is
        # ``True`` only when a flag was actually registered AND signalled
        # (i.e. the sub-agent really was in-flight). When the sub-agent has
        # already finished (no live flag), ``aborted`` is ``False`` and the
        # request is still ``ok`` (idempotent no-op).
        await container.chat.subagent_sessions.get(
            SubAgentSessionId.of(subagent_id),
        )
        aborted = container.chat.subagent_abort_registry.abort(subagent_id)
        return SubAgentInterruptResponse(ok=True, aborted=aborted)

    # ---- Multi-agent discussion: participant roster CRUD (block 4) -------
    # All four handlers are thin (interfaces-stays-thin): parse the request,
    # call one application use case, serialise the result. Business logic
    # (conversation existence check, id minting, PATCH-field application,
    # delete scoping) lives in ``participant_management`` use cases. New
    # routes only — no existing path / method / payload changed (§3.1).

    @router.get(
        "/conversations/{conversation_id}/participants",
        response_model=ParticipantListResponse,
    )
    async def list_participants(
        conversation_id: str,
    ) -> ParticipantListResponse:
        rows = await container.chat.list_participants_use_case.execute(
            ListParticipantsInput(
                conversation_id=ConversationId.of(conversation_id),
            ),
        )
        return ParticipantListResponse(
            items=[_participant_to_item(p) for p in rows],
        )

    @router.post(
        "/conversations/{conversation_id}/participants",
        response_model=ParticipantItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_participant(
        conversation_id: str,
        body: CreateParticipantRequest,
    ) -> ParticipantItem:
        participant = await container.chat.create_participant_use_case.execute(
            CreateParticipantInput(
                conversation_id=ConversationId.of(conversation_id),
                display_name=body.display_name,
                model_id=body.model_id,
                persona=body.persona,
                config=body.config.to_blob() if body.config is not None else None,
            ),
        )
        return _participant_to_item(participant)

    @router.patch(
        "/conversations/{conversation_id}/participants/{participant_id}",
        response_model=ParticipantItem,
    )
    async def update_participant(
        conversation_id: str,
        participant_id: str,
        body: UpdateParticipantRequest,
    ) -> ParticipantItem:
        # PATCH semantics: only fields the client actually sent are applied.
        # ``model_fields_set`` distinguishes "omitted" from "explicit null";
        # unset fields are left as the use case's ``_UNSET`` default.
        sent = body.model_fields_set
        kwargs: dict[str, Any] = {}
        if "display_name" in sent:
            kwargs["display_name"] = body.display_name
        if "model_id" in sent:
            kwargs["model_id"] = body.model_id
        if "persona" in sent:
            kwargs["persona"] = body.persona
        if "config" in sent:
            kwargs["config"] = (
                body.config.to_blob() if body.config is not None else None
            )
        participant = await container.chat.update_participant_use_case.execute(
            UpdateParticipantInput(
                conversation_id=ConversationId.of(conversation_id),
                participant_id=ParticipantId.of(participant_id),
                **kwargs,
            ),
        )
        return _participant_to_item(participant)

    @router.delete(
        "/conversations/{conversation_id}/participants/{participant_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model=None,
    )
    async def delete_participant(
        conversation_id: str,
        participant_id: str,
    ) -> None:
        await container.chat.delete_participant_use_case.execute(
            DeleteParticipantInput(
                conversation_id=ConversationId.of(conversation_id),
                participant_id=ParticipantId.of(participant_id),
            ),
        )
        return None

    # ---- Multi-agent discussion: conversation-level config (block 4) -----

    @router.get(
        "/conversations/{conversation_id}/discussion",
        response_model=DiscussionConfigBody,
    )
    async def get_discussion_config(
        conversation_id: str,
    ) -> DiscussionConfigBody:
        discussion = await container.chat.get_discussion_config_use_case.execute(
            GetDiscussionConfigInput(
                conversation_id=ConversationId.of(conversation_id),
            ),
        )
        d = discussion or {}
        return DiscussionConfigBody(
            is_discussion=bool(d.get("is_discussion", False)),
            selector_mode=d.get("selector_mode"),
            max_rounds=d.get("max_rounds"),
            enable_judge=d.get("enable_judge"),
            discussion_prompt=d.get("discussion_prompt"),
            selected_mode_id=d.get("selected_mode_id"),
            mode_selection_policy=d.get("mode_selection_policy"),
            convergence_control_enabled=d.get("convergence_control_enabled"),
            manager_early_end_enabled=d.get("manager_early_end_enabled"),
            soft_stop_enabled=d.get("soft_stop_enabled"),
            soft_stop_mode=d.get("soft_stop_mode"),
            social_response_policy=d.get("social_response_policy"),
            manager_prompt_customization_mode=d.get(
                "manager_prompt_customization_mode"
            ),
            manager_prompt_append=d.get("manager_prompt_append"),
            implementation_enabled=d.get("implementation_enabled"),
            intent_classifier_enabled=d.get("intent_classifier_enabled"),
            impl_max_total_file_edits=d.get("impl_max_total_file_edits"),
            impl_max_total_exec_calls=d.get("impl_max_total_exec_calls"),
            impl_max_total_runtime_seconds=d.get(
                "impl_max_total_runtime_seconds"
            ),
            impl_max_total_changed_files=d.get("impl_max_total_changed_files"),
            soft_stop_similarity=d.get("soft_stop_similarity"),
            soft_stop_min_rounds=d.get("soft_stop_min_rounds"),
            soft_stop_consecutive_turns=d.get("soft_stop_consecutive_turns"),
            intent_classifier_model=d.get("intent_classifier_model"),
            intent_classifier_timeout_ms=d.get("intent_classifier_timeout_ms"),
            implementation_planner_model=d.get("implementation_planner_model"),
            implementation_planner_timeout_ms=d.get(
                "implementation_planner_timeout_ms"
            ),
            implementation_validator_enabled=d.get(
                "implementation_validator_enabled"
            ),
            implementation_validator_timeout_ms=d.get(
                "implementation_validator_timeout_ms"
            ),
            implementation_verify_command_timeout_ms=d.get(
                "implementation_verify_command_timeout_ms"
            ),
        )

    @router.patch(
        "/conversations/{conversation_id}/discussion",
        response_model=DiscussionConfigBody,
    )
    async def set_discussion_config(
        conversation_id: str,
        body: DiscussionConfigBody,
    ) -> DiscussionConfigBody:
        discussion = await container.chat.set_discussion_config_use_case.execute(
            SetDiscussionConfigInput(
                conversation_id=ConversationId.of(conversation_id),
                is_discussion=body.is_discussion,
                selector_mode=body.selector_mode,
                max_rounds=body.max_rounds,
                enable_judge=body.enable_judge,
                discussion_prompt=body.discussion_prompt,
                selected_mode_id=body.selected_mode_id,
                mode_selection_policy=body.mode_selection_policy,
                convergence_control_enabled=body.convergence_control_enabled,
                manager_early_end_enabled=body.manager_early_end_enabled,
                soft_stop_enabled=body.soft_stop_enabled,
                soft_stop_mode=body.soft_stop_mode,
                social_response_policy=body.social_response_policy,
                manager_prompt_customization_mode=(
                    body.manager_prompt_customization_mode
                ),
                manager_prompt_append=body.manager_prompt_append,
                implementation_enabled=body.implementation_enabled,
                intent_classifier_enabled=body.intent_classifier_enabled,
                impl_max_total_file_edits=body.impl_max_total_file_edits,
                impl_max_total_exec_calls=body.impl_max_total_exec_calls,
                impl_max_total_runtime_seconds=(
                    body.impl_max_total_runtime_seconds
                ),
                impl_max_total_changed_files=body.impl_max_total_changed_files,
                soft_stop_similarity=body.soft_stop_similarity,
                soft_stop_min_rounds=body.soft_stop_min_rounds,
                soft_stop_consecutive_turns=body.soft_stop_consecutive_turns,
                intent_classifier_model=body.intent_classifier_model,
                intent_classifier_timeout_ms=body.intent_classifier_timeout_ms,
                implementation_planner_model=body.implementation_planner_model,
                implementation_planner_timeout_ms=(
                    body.implementation_planner_timeout_ms
                ),
                implementation_validator_enabled=(
                    body.implementation_validator_enabled
                ),
                implementation_validator_timeout_ms=(
                    body.implementation_validator_timeout_ms
                ),
                implementation_verify_command_timeout_ms=(
                    body.implementation_verify_command_timeout_ms
                ),
            ),
        )
        d = discussion or {}
        return DiscussionConfigBody(
            is_discussion=bool(d.get("is_discussion", False)),
            selector_mode=d.get("selector_mode"),
            max_rounds=d.get("max_rounds"),
            enable_judge=d.get("enable_judge"),
            discussion_prompt=d.get("discussion_prompt"),
            selected_mode_id=d.get("selected_mode_id"),
            mode_selection_policy=d.get("mode_selection_policy"),
            convergence_control_enabled=d.get("convergence_control_enabled"),
            manager_early_end_enabled=d.get("manager_early_end_enabled"),
            soft_stop_enabled=d.get("soft_stop_enabled"),
            soft_stop_mode=d.get("soft_stop_mode"),
            social_response_policy=d.get("social_response_policy"),
            manager_prompt_customization_mode=d.get(
                "manager_prompt_customization_mode"
            ),
            manager_prompt_append=d.get("manager_prompt_append"),
            implementation_enabled=d.get("implementation_enabled"),
            intent_classifier_enabled=d.get("intent_classifier_enabled"),
            impl_max_total_file_edits=d.get("impl_max_total_file_edits"),
            impl_max_total_exec_calls=d.get("impl_max_total_exec_calls"),
            impl_max_total_runtime_seconds=d.get(
                "impl_max_total_runtime_seconds"
            ),
            impl_max_total_changed_files=d.get("impl_max_total_changed_files"),
            soft_stop_similarity=d.get("soft_stop_similarity"),
            soft_stop_min_rounds=d.get("soft_stop_min_rounds"),
            soft_stop_consecutive_turns=d.get("soft_stop_consecutive_turns"),
            intent_classifier_model=d.get("intent_classifier_model"),
            intent_classifier_timeout_ms=d.get("intent_classifier_timeout_ms"),
            implementation_planner_model=d.get("implementation_planner_model"),
            implementation_planner_timeout_ms=d.get(
                "implementation_planner_timeout_ms"
            ),
            implementation_validator_enabled=d.get(
                "implementation_validator_enabled"
            ),
            implementation_validator_timeout_ms=d.get(
                "implementation_validator_timeout_ms"
            ),
            implementation_verify_command_timeout_ms=d.get(
                "implementation_verify_command_timeout_ms"
            ),
        )

    # ---- Multi-agent discussion: implementation plan read + edit (§22.9) -

    @router.get(
        "/conversations/{conversation_id}/implementation",
        response_model=ImplementationPlanBody,
    )
    async def get_implementation_plan(
        conversation_id: str,
    ) -> ImplementationPlanBody:
        discussion = (
            await container.chat.get_implementation_plan_use_case.execute(
                GetImplementationPlanInput(
                    conversation_id=ConversationId.of(conversation_id),
                ),
            )
        )
        # No plan (flag-OFF / pre-extraction) → stable empty shell (phase=none).
        return _discussion_to_plan_body(discussion)

    @router.patch(
        "/conversations/{conversation_id}/implementation",
        response_model=ImplementationPlanBody,
    )
    async def update_implementation_plan(
        conversation_id: str,
        body: ImplementationPlanPatchBody,
    ) -> ImplementationPlanBody:
        # An illegal assigned_role raises ValidationError (→ 400); editing the
        # in-flight item while implementing raises ConflictError (→ 409). Both
        # propagate to the unified error handler — no per-route HTTPException.
        discussion = (
            await container.chat.update_implementation_plan_use_case.execute(
                UpdateImplementationPlanInput(
                    conversation_id=ConversationId.of(conversation_id),
                    items=tuple(
                        item.model_dump() for item in body.items
                    ),
                ),
            )
        )
        return _discussion_to_plan_body(discussion)

    return router


__all__ = ["build_router"]
