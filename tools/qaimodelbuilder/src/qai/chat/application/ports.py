# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application-layer ports for the chat bounded context.

Ports are abstract :class:`typing.Protocol` types that describe the
external dependencies required by chat use cases.  Concrete adapters
live in ``qai.chat.adapters`` (S4 PR-040+) and ``qai.chat.infrastructure``
(S4) -- this PR (S2 PR-021) defines the interfaces only.

Design rules (from the S2 sub-agent spec §4.1):

* All ports are :class:`typing.Protocol` types.
* Methods carry full type annotations and a docstring.
* Cross-context coordination MUST go through these ports rather than
  direct imports of other contexts.

Ports defined here:

* :class:`ConversationRepositoryPort` -- conversation CRUD + paged
  message reads + textual search.
* :class:`ExperienceRepositoryPort` -- experience-library CRUD.
* :class:`LLMStreamPort` -- abstract async-streaming LLM call; the
  use case orchestrates frames coming out of this port without
  caring about the underlying SSE / WebSocket / HTTP transport.
* :class:`ToolInvocationPort` -- abstracted entry point to the tools
  bounded context (chat does not import ``qai.tools.*`` directly).
* :class:`TabSessionStorePort` -- server-side mirror of front-end tab
  state, supporting parallel multi-tab conversations.
* :class:`StreamAbortRegistryPort` -- registry of in-flight stream
  handles keyed by ``TabId``; supports cooperative cancellation.

Notes
-----

* All async iterators returned by ports yield :class:`StreamFrame`
  values; transport encoding (SSE wire format, WS framing) is the
  adapter's job.
* Repositories return rich domain types; they MUST NOT leak DB rows
  or pydantic models.
* Errors raised by adapters are expected to be subclasses of
  :class:`qai.platform.errors.QaiError`; chat use cases translate
  them into chat-specific domain errors where appropriate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from qai.chat.domain.agent_template import AgentTemplate
from qai.chat.domain.budget import BudgetCheckResult
from qai.chat.domain.content import ContextSize, MessageContent
from qai.chat.domain.conversation import Conversation
from qai.chat.domain.experience import Experience
from qai.chat.domain.hook import HookConfig, HookDecision, HookEvent
from qai.chat.domain.ids import (
    AgentTemplateId,
    ConversationId,
    ExperienceId,
    MessageId,
    ModeTemplateId,
    ParticipantId,
    RosterTemplateId,
    SubAgentSessionId,
    TabId,
)
from qai.chat.domain.message import Message
from qai.chat.domain.mode_template import ModeTemplate
from qai.chat.domain.participant import Participant
from qai.chat.domain.roster_template import RosterTemplate
from qai.chat.domain.stream_frame import StreamFrame
from qai.chat.domain.sub_agent_session import SubAgentSession
from qai.chat.domain.tab import ConversationTab

if TYPE_CHECKING:  # pragma: no cover
    # CCD-5: the compaction checkpoint dataclass lives in the same application
    # layer (``use_cases/_agentic_kernel.py``), but that module imports FROM
    # this one, so a module-level import here would be circular. Reference it
    # only under TYPE_CHECKING + string annotations (PEP 563 ``from __future__
    # import annotations`` is already in effect, so the runtime never resolves
    # the name). This keeps the port definition pure / import-cycle-free.
    from qai.chat.application.use_cases._agentic_kernel import (
        CompactionCheckpoint,
    )
    from qai.chat.domain.mcp_catalog import CuratedCatalogEntry
    from qai.chat.domain.mcp_server import (
        McpPrompt,
        McpResource,
        McpServerConfig,
    )


# ---------------------------------------------------------------------------
# Conversation repository
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ConversationListItem:
    """Lightweight projection used by listing endpoints.

    Excludes the (potentially long) message history -- callers pull
    that via :meth:`ConversationRepositoryPort.fetch_messages_page`
    when needed.
    """

    conversation: Conversation
    message_count: int
    # V1-parity sidebar badges (history_store.py:216-230): per-conversation
    # user-turn count and tool-call count. Appended with defaults so existing
    # constructions / in-memory fakes stay valid.
    round_count: int = 0
    tool_call_count: int = 0
    # Sub-agent count for this conversation (number of persisted
    # ``chat_subagent_session`` rows whose parent is this conversation).
    # Appended with default 0 (AGENTS.md §3.1) so the sidebar can hide the
    # "expand sub-agents" affordance for conversations that spawned none.
    subagent_count: int = 0
    # V1-parity search snippet (history_store.py:702): an HTML fragment of the
    # best-matching message body with the matched terms wrapped in
    # ``<mark>...</mark>`` (the front-end renders it via ``v-html``). Empty for
    # plain ``list()`` projections and for ``search()`` rows that only matched
    # the conversation title (no message-body hit to excerpt). Appended with a
    # default so existing constructions / in-memory fakes stay valid
    # (v2.7 §3.1 — namespace fields locked, appending allowed).
    snippet: str = ""


@dataclass(frozen=True, slots=True)
class MessagesPage:
    """A page of messages returned by the repository.

    ``next_cursor`` is an opaque string the caller passes back to fetch
    the next page; ``None`` means the end of the conversation has been
    reached.
    """

    items: tuple[Message, ...]
    next_cursor: str | None


@runtime_checkable
class ConversationRepositoryPort(Protocol):
    """Persistence port for :class:`Conversation` aggregates."""

    async def save(self, conversation: Conversation) -> None:
        """Insert or upsert ``conversation`` (by id).

        Writes the conversation HEADER (title / status / meta) from the
        in-memory aggregate. Use from header-owning use cases (create /
        rename / set-workspace / auto-title).
        """
        ...

    async def save_messages(self, conversation: Conversation) -> None:
        """Persist messages + status, PRESERVING the DB header on an
        existing row (title / meta are not overwritten from the aggregate).

        Use from turn persistence (streaming appends) so a concurrent manual
        rename mid-turn is never rolled back by a stale snapshot
        (State-Truth-First). On a brand-new row the aggregate's title/meta
        are still seeded.
        """
        ...

    async def get(self, conversation_id: ConversationId) -> Conversation:
        """Return the aggregate; raise ``ConversationNotFoundError`` if missing."""
        ...

    async def find(
        self,
        conversation_id: ConversationId,
    ) -> Conversation | None:
        """Return the aggregate or ``None`` if not present."""
        ...

    async def find_latest_by_channel_user(
        self,
        source: str,
        channel_user_id: str,
    ) -> Conversation | None:
        """Return the most-recently-updated conversation for a channel user.

        Tail-appended read method (AGENTS.md §3.1 — only adds a query, no
        existing signature changes).  Restores V1
        ``history_store.get_latest_wechat_conversation`` /
        ``get_latest_feishu_conversation``
        (``QAIModelBuilder_v0.5_pure/backend/channels/.../history_store.py:363-405``):
        matches conversations whose ``meta`` carries the given
        ``{"source": ..., "channel_user_id": ...}`` pair, ordered by
        ``updated_at`` DESC, returning the newest non-archived one (or
        ``None``).  Used by the channels→chat bridge so a service restart can
        resume the SAME conversation (and its history) for the SAME channel
        user instead of minting a fresh one (the in-memory
        ``_USER_CONV_IDS`` cache is lost on restart).
        """
        ...

    async def delete(self, conversation_id: ConversationId) -> None:
        """Remove the conversation; raise ``ConversationNotFoundError`` if missing."""
        ...

    async def find_ids_by_selected_mode(
        self,
        mode_id: str,
    ) -> tuple[ConversationId, ...]:
        """Return the ids of conversations currently selecting ``mode_id``.

        Tail-appended read method (AGENTS.md §3.1 — only adds a query). Matches
        rows whose ``meta_json`` carries
        ``$.discussion.selected_mode_id == mode_id``. Used by the mode-template
        delete flow (decision 7): count for the confirm dialog, and the cleanup
        that reverts those conversations to the sentinel ("跟随默认") after the
        mode is deleted. Returns an empty tuple when no conversation uses it.
        """
        ...

    async def clear_request_ids(self) -> int:
        """Strip persisted ``meta.request_id`` from all messages at startup.

        Prompt-snapshot parity (9-G1): the in-memory snapshot store is empty
        after a restart, so leftover ``request_id`` values point at snapshots
        that no longer exist. Returns the number of rows updated.
        """
        ...

    async def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        favorite_only: bool = False,
        pinned_only: bool = False,
    ) -> tuple[ConversationListItem, ...]:
        """Return conversations ordered by ``updated_at`` DESC.

        ``favorite_only`` / ``pinned_only`` restrict the result to rows whose
        ``meta_json`` carries a truthy ``favorite`` / ``pinned`` flag. The
        favorites dialog uses ``favorite_only`` to fetch the COMPLETE set of
        favorited conversations (not just the recent ``limit`` window the
        sidebar shows). Filters are AND-combined when both are set.
        """
        ...

    async def search(
        self,
        *,
        query: str,
        limit: int = 50,
    ) -> tuple[ConversationListItem, ...]:
        """Free-text search over title and message text."""
        ...

    async def fetch_messages_page(
        self,
        *,
        conversation_id: ConversationId,
        cursor: str | None = None,
        limit: int = 50,
    ) -> MessagesPage:
        """Return a page of messages for the given conversation."""
        ...


# ---------------------------------------------------------------------------
# Sub-agent session repository
# ---------------------------------------------------------------------------
@runtime_checkable
class SubAgentSessionRepositoryPort(Protocol):
    """Persistence port for :class:`SubAgentSession` aggregates (sub-agent context/memory).

    子 Agent 会话承载子 Agent 的持久化 wire 上下文，支持主 Agent 用 task_id 风格
    唤醒续做 + 用户接管对话。跟随父会话生命周期（父删级联删）。
    """

    async def save(self, session: SubAgentSession) -> None:
        """Insert or upsert ``session`` (by id)."""
        ...

    async def get(self, session_id: SubAgentSessionId) -> SubAgentSession:
        """Return the aggregate; raise ``SubAgentSessionNotFoundError`` if missing."""
        ...

    async def find(
        self,
        session_id: SubAgentSessionId,
    ) -> SubAgentSession | None:
        """Return the aggregate or ``None`` if not present."""
        ...

    async def delete(self, session_id: SubAgentSessionId) -> None:
        """Remove the session; raise ``SubAgentSessionNotFoundError`` if missing."""
        ...

    async def list_by_root_conversation(
        self,
        root_conversation_id: ConversationId,
    ) -> tuple[SubAgentSession, ...]:
        """Return all sub-agent sessions under a ROOT conversation, ordered
        by ``created_at`` ASC.

        A "root conversation" is the top-of-tree main-agent conversation. Every
        sub-agent under that root — at every depth — carries the same
        ``root_conversation_id``, so this method returns the FULL sub-agent
        forest of the conversation (depth 1, 2, 3, … all mixed, sorted by time
        created). Use :meth:`list_by_parent_subagent` to walk one specific
        parent's direct children.
        """
        ...

    async def list_by_parent_subagent(
        self,
        parent_subagent_id: SubAgentSessionId,
    ) -> tuple[SubAgentSession, ...]:
        """Return the direct children of one sub-agent (created_at ASC).

        A sub-agent's direct children are the sub-agents it spawned itself — one
        level below it in the tree. When ``parent_subagent_id`` is unknown /
        missing, returns an empty tuple (best-effort; walking a non-existent
        parent yields no children, not an error).
        """
        ...

    async def delete_by_parent(
        self,
        root_conversation_id: ConversationId,
    ) -> int:
        """Cascade-delete every sub-agent session under a root conversation.

        Returns the number of rows removed. Used when the parent conversation
        is deleted so the entire sub-agent forest (all depths) of that root
        is cleaned up in one shot. The parameter is the ROOT (top-of-tree)
        conversation id — the column all sub-agent rows share regardless of
        depth.
        """
        ...


# ---------------------------------------------------------------------------
# Compaction-checkpoint store (CCD-5)
# ---------------------------------------------------------------------------
@runtime_checkable
class CompactionCheckpointStorePort(Protocol):
    """Persistence port for the session-level compaction checkpoint (CCD-5).

    One checkpoint per conversation. :class:`StreamChatUseCase` keeps an
    in-process ``dict`` of checkpoints as the WRITE-THROUGH fast path; this
    port is the durable backing store so a checkpoint survives a process
    restart (PENDING-WORK.md §1 CCD-5). Without it, a restart drops every
    checkpoint → a long conversation's first post-restart message falls back
    to the full verbatim history (risking PROMPT_TOO_LONG → forced
    recompaction) and the differential token baseline is re-bootstrapped from
    char estimates.

    The checkpoint follows the conversation's lifecycle: :meth:`delete`
    removes it explicitly, and the DB-level ``ON DELETE CASCADE`` on
    ``chat_compaction_checkpoint.conversation_id`` removes it automatically
    when the parent conversation row is deleted (``foreign_keys=ON``).

    ``checkpoint`` is annotated as a forward reference (string) because the
    concrete :class:`CompactionCheckpoint` dataclass lives in
    ``use_cases/_agentic_kernel.py`` which imports from this module — see the
    ``TYPE_CHECKING`` note at the top of the file.
    """

    async def save(
        self,
        conversation_id: ConversationId,
        checkpoint: "CompactionCheckpoint",
    ) -> None:
        """Insert or upsert the conversation's checkpoint (single-row aggregate)."""
        ...

    async def load(
        self,
        conversation_id: ConversationId,
    ) -> "CompactionCheckpoint | None":
        """Return the conversation's checkpoint or ``None`` if none persisted."""
        ...

    async def delete(self, conversation_id: ConversationId) -> None:
        """Remove the conversation's checkpoint (idempotent — no error if absent)."""
        ...


# ---------------------------------------------------------------------------
# Participant repository
# ---------------------------------------------------------------------------
@runtime_checkable
class ParticipantRepositoryPort(Protocol):
    """Persistence port for :class:`Participant` aggregates.

    会话参与者通用抽象：今天承载子 Agent（kind=sub_agent），未来承载多 Agent
    对话的具名角色（kind=named_agent）。role 维度不变，participant 是正交维度。
    """

    async def save(self, participant: Participant) -> None:
        """Insert or upsert ``participant`` (by id)."""
        ...

    async def get(self, participant_id: ParticipantId) -> Participant:
        """Return the aggregate; raise ``ParticipantNotFoundError`` if missing."""
        ...

    async def find(
        self,
        participant_id: ParticipantId,
    ) -> Participant | None:
        """Return the aggregate or ``None`` if not present."""
        ...

    async def list_by_conversation(
        self,
        conversation_id: ConversationId,
    ) -> tuple[Participant, ...]:
        """Return all participants of the given conversation."""
        ...

    async def delete(self, participant_id: ParticipantId) -> None:
        """Remove the participant; raise ``ParticipantNotFoundError`` if missing."""
        ...


# ---------------------------------------------------------------------------
# Roster-template repository
# ---------------------------------------------------------------------------
@runtime_checkable
class RosterTemplateRepositoryPort(Protocol):
    """Persistence port for :class:`RosterTemplate` aggregates.

    角色模板库（可复用的多 Agent 讨论"团队"）。与 ``ParticipantRepositoryPort``
    正交：participant 严格绑会话，roster template 是会话无关的全局库条目，承载
    角色定义（display_name / model_id / persona / config），"应用"到会话时再实例
    化成 ``kind=named_agent`` 的 participant 行。
    """

    async def save(self, template: RosterTemplate) -> None:
        """Insert or upsert ``template`` (single-row aggregate, by id)."""
        ...

    async def get(self, template_id: RosterTemplateId) -> RosterTemplate:
        """Return the aggregate; raise ``RosterTemplateNotFoundError`` if missing."""
        ...

    async def find(
        self,
        template_id: RosterTemplateId,
    ) -> RosterTemplate | None:
        """Return the aggregate or ``None`` if not present."""
        ...

    async def list_all(self) -> tuple[RosterTemplate, ...]:
        """Return every roster template (built-ins first, then by created_at)."""
        ...

    async def delete(self, template_id: RosterTemplateId) -> None:
        """Remove the template; raise ``RosterTemplateNotFoundError`` if missing."""
        ...


# ---------------------------------------------------------------------------
# Agent-template repository (single-role library)
# ---------------------------------------------------------------------------
@runtime_checkable
class AgentTemplateRepositoryPort(Protocol):
    """Persistence port for :class:`AgentTemplate` aggregates.

    单角色模板库（可复用的单个讨论角色"agent"）。是三层模板体系（§27：单角色 →
    团队 → 模式）里最小的复用单元，与团队级 ``RosterTemplateRepositoryPort`` 正交。
    与 ``ParticipantRepositoryPort`` 同样正交：participant 严格绑会话，agent
    template 是会话无关的全局库条目，承载单个角色定义（display_name / model_id /
    persona / config），"应用"到会话时实例化成一个 ``kind=named_agent`` 的
    participant 行。
    """

    async def save(self, template: AgentTemplate) -> None:
        """Insert or upsert ``template`` (single-row aggregate, by id)."""
        ...

    async def get(self, template_id: AgentTemplateId) -> AgentTemplate:
        """Return the aggregate; raise ``AgentTemplateNotFoundError`` if missing."""
        ...

    async def find(
        self,
        template_id: AgentTemplateId,
    ) -> AgentTemplate | None:
        """Return the aggregate or ``None`` if not present."""
        ...

    async def list_all(self) -> tuple[AgentTemplate, ...]:
        """Return every agent template (built-ins first, then by created_at)."""
        ...

    async def delete(self, template_id: AgentTemplateId) -> None:
        """Remove the template; raise ``AgentTemplateNotFoundError`` if missing."""
        ...


# ---------------------------------------------------------------------------
# Mode-template repository (collaboration-mode library)
# ---------------------------------------------------------------------------
@runtime_checkable
class ModeTemplateRepositoryPort(Protocol):
    """Persistence port for :class:`ModeTemplate` aggregates.

    协作模式模板库（"怎么协作"：讨论 / 评审 / 辩论 / 实施 / 自定义）。是三层模板
    体系（§26 / §27）的第三层，与团队模板（``RosterTemplateRepositoryPort``）正交：
    团队回答"谁参与"，模式回答"怎么协作"。会话经 ``meta["discussion"]
    ["selected_mode_id"]`` 引用所选模式（尾部追加，§3.1），不在此建 FK。
    """

    async def save(self, template: ModeTemplate) -> None:
        """Insert or upsert ``template`` (single-row aggregate, by id)."""
        ...

    async def get(self, template_id: ModeTemplateId) -> ModeTemplate:
        """Return the aggregate; raise ``ModeTemplateNotFoundError`` if missing."""
        ...

    async def find(
        self,
        template_id: ModeTemplateId,
    ) -> ModeTemplate | None:
        """Return the aggregate or ``None`` if not present."""
        ...

    async def list_all(self) -> tuple[ModeTemplate, ...]:
        """Return every mode template (built-ins first, then by created_at)."""
        ...

    async def delete(self, template_id: ModeTemplateId) -> None:
        """Remove the template; raise ``ModeTemplateNotFoundError`` if missing."""
        ...


# ---------------------------------------------------------------------------
# Experience repository
# ---------------------------------------------------------------------------
@runtime_checkable
class ExperienceRepositoryPort(Protocol):
    """Persistence port for the experience library."""

    async def save(self, experience: Experience) -> None:
        """Insert or upsert ``experience`` (by id)."""
        ...

    async def get(self, experience_id: ExperienceId) -> Experience:
        """Return the experience; raise ``ExperienceNotFoundError`` if missing."""
        ...

    async def delete(self, experience_id: ExperienceId) -> None:
        """Remove the experience; raise ``ExperienceNotFoundError`` if missing."""
        ...

    async def list(
        self,
        *,
        category: str | None = None,
        limit: int = 50,
    ) -> tuple[Experience, ...]:
        """Return experiences, optionally filtered by category."""
        ...

    async def list_categories(self) -> tuple[str, ...]:
        """Return distinct categories currently in use."""
        ...


# ---------------------------------------------------------------------------
# LLM streaming
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class LLMStreamRequest:
    """Inputs to :meth:`LLMStreamPort.stream`.

    The chat use case constructs this; adapters consume it.  Keeping
    it a frozen dataclass (rather than dict) makes call sites easier to
    type-check and gives a single place to evolve the contract.
    """

    conversation_id: ConversationId
    tab_id: TabId
    prompt: MessageContent
    history: tuple[Message, ...]
    model_hint: str | None = None
    extra: dict[str, Any] | None = None


@runtime_checkable
class LLMStreamPort(Protocol):
    """Abstract LLM streaming call.

    The adapter returns an :class:`AsyncIterator` of :class:`StreamFrame`;
    each yielded frame is one logical event (chunk / tool_call /
    tool_result / error / end).  The adapter MUST emit at most one
    ``END`` frame as the final yield of the iterator (the use case
    relies on this to know the stream completed normally).
    """

    def stream(
        self,
        request: LLMStreamRequest,
    ) -> AsyncIterator[StreamFrame]:
        """Open an LLM stream and yield :class:`StreamFrame` values."""
        ...


# ---------------------------------------------------------------------------
# Tool invocation (cross-context boundary)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ToolInvocationRequest:
    """Inputs to :meth:`ToolInvocationPort.invoke`."""

    tab_id: TabId
    conversation_id: ConversationId
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolInvocationResult:
    """Outputs of :meth:`ToolInvocationPort.invoke`."""

    tool_name: str
    ok: bool
    result: Any
    error_code: str | None = None
    error_message: str | None = None


# ----- Streaming tool output (WIRE-tools) -----
class ToolStreamChunkKind(str, Enum):
    """Discriminator for a :class:`ToolStreamChunk`."""

    #: An incremental stdout line/chunk produced while the tool runs.
    STDOUT = "stdout"
    #: An incremental stderr line/chunk produced while the tool runs.
    STDERR = "stderr"
    #: The terminal chunk: the tool finished; ``result`` / ``ok`` carry the
    #: consolidated outcome the use case feeds back to the LLM.
    DONE = "done"


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolStreamChunk:
    """One frame yielded by :meth:`ToolInvocationPort.invoke_streaming`.

    WIRE-tools streaming contract.  The chat use case detects the optional
    :meth:`ToolInvocationPort.invoke_streaming` capability (via ``getattr``)
    and, when present and the tool supports streaming (e.g. ``exec``),
    drives this iterator instead of the one-shot :meth:`invoke`.  Each
    :data:`ToolStreamChunkKind.STDOUT` / :data:`STDERR` chunk carries a
    ``text`` delta the use case forwards as a ``partial=True``
    :meth:`~qai.chat.domain.stream_frame.StreamFrame.tool_result` frame
    (V1 ``backend/tools/_exec.py:1010`` ``{type:"output"}`` parity); the
    single terminal :data:`DONE` chunk carries the consolidated
    ``result`` / ``ok`` / ``original_length`` / ``truncated`` that become
    the final ``partial=False`` frame fed back to the LLM.

    Fields:

    * ``kind`` — STDOUT / STDERR / DONE discriminator.
    * ``text`` — the delta text for STDOUT / STDERR chunks; empty for DONE.
    * ``result`` — only meaningful on the DONE chunk: the consolidated
      tool output (the full text the LLM should see, *before* the chat
      use case applies its own :class:`ToolResultTruncatorPort`).
    * ``ok`` — only meaningful on DONE: whether the tool succeeded.
    * ``original_length`` / ``truncated`` — optional DONE metadata the
      adapter may pre-compute; the use case may override after its own
      truncation.  ``None`` means "let the use case decide".
    """

    kind: ToolStreamChunkKind
    text: str = ""
    result: Any = None
    ok: bool = True
    original_length: int | None = None
    truncated: bool | None = None


@runtime_checkable
class ToolInvocationPort(Protocol):
    """Abstract entry point for invoking a tool from the chat context.

    The chat context never imports ``qai.tools.*`` directly -- it goes
    through this port so the wiring can route to whichever execution
    strategy (sandboxed subprocess, in-process callable, remote MCP
    server, ...) the deployment uses.
    """

    async def invoke(
        self,
        request: ToolInvocationRequest,
    ) -> ToolInvocationResult:
        """Execute the named tool and return a structured result."""
        ...

    def schemas(self) -> tuple[dict[str, Any], ...]:
        """Return the OpenAI function-calling schemas for registered tools.

        Each entry is an OpenAI ``tools[]`` element of the shape::

            {"type": "function", "function": {"name", "description", "parameters"}}

        Returned as a tuple so callers can hand it directly to the LLM
        adapter (which sets ``payload["tools"] = list(schemas)``).  An
        empty tuple means the registry holds no schema-bearing tools and
        the caller should NOT advertise any tools to the LLM (cloud
        models then fall back to plain text completion).

        Additive method (v2.7 §3.1: existing namespace fields locked,
        appending allowed).  Stubs / fakes that predate this method are
        called via ``getattr(tools, "schemas", None)`` so they keep
        working without forced updates.
        """
        ...

    def cloud_description_overrides(self) -> dict[str, dict[str, Any]]:
        """Return cloud-only tool description overrides, keyed by tool name.

        Shape per entry: ``{"description": str, "param_descriptions": {field: str}}``
        (``param_descriptions`` optional). Cloud LLM turns overlay these richer,
        guidance-heavy descriptions onto the advertised schemas; on-device turns
        keep the short registered text (small models are sensitive to prompt bloat).

        Additive method (v2.7 §3.1). Called via
        ``getattr(tools, "cloud_description_overrides", None)`` so stubs/fakes
        predating it keep working (treated as "no overrides").
        """
        ...

    def invoke_streaming(
        self,
        request: ToolInvocationRequest,
    ) -> AsyncIterator[ToolStreamChunk] | None:
        """Stream a tool's output as it is produced, or ``None`` to opt out.

        **Optional, tail-appended capability** (AGENTS.md §3.1: the
        existing :meth:`invoke` signature is unchanged; this is a new
        method with a ``None`` opt-out so pre-existing adapters / fakes
        that do not implement it keep working — the chat use case probes
        for it with ``getattr(tools, "invoke_streaming", None)`` and falls
        back to :meth:`invoke` when absent).

        WIRE-tools: V1's exec tool streamed stdout/stderr line-by-line
        (``backend/tools/_exec.py:1010`` ``{type:"output"}`` frames) so
        the WebUI rendered output live; the V2 :meth:`invoke` collapses
        the whole run into one return value.  This method lets
        streaming-capable tools (today: ``exec``) yield
        :class:`ToolStreamChunk` increments followed by one terminal
        ``DONE`` chunk.

        Contract:

        * Return ``None`` (synchronously) when ``request.tool_name`` is
          **not** a streaming-capable tool — the use case then drives the
          one-shot :meth:`invoke` path with byte-for-byte prior behaviour.
        * Otherwise return an :class:`AsyncIterator` that yields zero or
          more :data:`ToolStreamChunkKind.STDOUT` / :data:`STDERR` chunks
          and **exactly one** terminal :data:`ToolStreamChunkKind.DONE`
          chunk as its final item (the use case relies on this to know
          the consolidated result).
        * Implementations MUST NOT raise from the factory call itself;
          per-chunk failures should surface as a DONE chunk with
          ``ok=False`` and an error ``result`` string.
        """
        ...


# ---------------------------------------------------------------------------
# Sub-agent event stream (V1 chat_handler.py:2188-2343 parity)
# ---------------------------------------------------------------------------
@runtime_checkable
class SubAgentEventStreamPort(Protocol):
    """Stream structured sub-agent events for an ``agent`` tool dispatch.

    The default ``ToolInvocationPort.invoke`` path collapses a sub-agent
    run into a single string return value (the consolidated text), which
    means the parent stream cannot surface the sub-agent's incremental
    output / tool calls / error to the UI.  V1 (``chat_handler.py:2188-
    2343``) instead yielded structured events
    (``subagent_start`` / ``subagent_output`` / ``subagent_tool`` /
    ``subagent_done`` / ``subagent_error``) so the frontend
    (``useChat.js:1345-1408``) could render an in-progress block per
    sub-agent.

    This port lifts that surface into a chat-context Protocol.  When
    wired on :class:`StreamChatUseCase`, the use case detects an
    ``agent`` ``tool_call`` frame inside its agentic loop and dispatches
    via :meth:`iter_events` instead of ``ToolInvocationPort.invoke``;
    each yielded event is translated into the matching
    :data:`StreamFrameType.SUBAGENT_*` :class:`StreamFrame` and forwarded
    to the parent stream consumer.

    Implementations must:

    * Always begin with one ``{"type": "subagent_start", "index": N,
      "total": M, "prompt_preview": "..."}`` event.
    * Always end with either ``{"type": "subagent_done", ...}`` or
      ``{"type": "subagent_error", ...}``.
    * Carry ``index`` (0-based) on every per-agent event so multiple
      parallel sub-agents in one parent turn can be discriminated.

    The reference implementation is :class:`qai.chat.adapters.agent_tool.
    AgentToolHandler.iter_events`.
    """

    def iter_events(
        self,
        request: ToolInvocationRequest,
        *,
        agent_index: int = 0,
        total_agents: int = 1,
        model_hint: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run one sub-agent and yield its event dicts.

        Field shapes mirror V1 wire shape verbatim
        (``backend/chat_handler.py:2204-2343``):

        * ``subagent_start``  ``{type, index, total, prompt_preview,
          subagent_id?, subagent_type?, name?}``
        * ``subagent_output`` ``{type, index, content}``
        * ``subagent_tool``   ``{type, index, tool_name, tool_args}``
        * ``subagent_done``   ``{type, index, result, rounds, subagent_id?}``
        * ``subagent_error``  ``{type, index, message}``

        ``model_hint`` carries the model id selected for the *parent*
        turn so the sub-agent's LLM calls route to the **same** endpoint
        the parent used (V1 parity: ``chat_handler.py:624-632`` passes
        the parent's ``resolved_model_id`` / ``model_provider`` /
        ``is_local`` straight into ``run_subagent``).  When ``None`` the
        sub-agent falls back to the default LLM stream — which on a dev
        box with no ``llm_base_url`` configured yields the offline notice
        ``[no LLM endpoint configured]``.

        The reference implementation accepts further optional keyword
        arguments (``tool_mode`` / ``workspace_root`` / ``resume_session_id``
        / ``subagent_type`` / ``subagent_name`` / ``allow_spawn`` /
        ``disabled_tools`` / ``spawn_depth`` / ``parent_subagent_id``) that
        the use case forwards; they are not part of this minimal Protocol
        surface so light test doubles need not declare them.

        ``spawn_depth`` (unified spawn path — alpha step): the recursion depth
        of the sub-agent being spawned by this call. The main agent's dispatch
        passes ``spawn_depth=1`` (a first-level sub-agent); an
        ``AgentToolHandler`` running its OWN ``agent``-tool spawn re-enters
        ``iter_events`` with ``spawn_depth=my_depth+1`` (a grand sub-agent is
        depth 2, great-grand depth 3, …). The reference implementation
        rejects a call with ``spawn_depth >= Settings.chat_max_spawn_depth``
        with a diagnostic error result — recursion 封顶 without a hard
        ``allow_spawn=False`` in the code path.

        ``parent_subagent_id`` (unified spawn path — alpha step): the id of the
        DIRECT parent sub-agent when the caller is itself a sub-agent (a
        grand / great-grand spawn), so the newly-persisted
        :class:`SubAgentSession` records its tree edge. ``None`` on the main
        agent's dispatch (my direct parent IS the main agent, which lives
        outside the sub-agent table).
        """
        ...


# ---------------------------------------------------------------------------
# Tab-session store
# ---------------------------------------------------------------------------
@runtime_checkable
class TabSessionStorePort(Protocol):
    """Server-side mirror of the front-end tab state.

    Backing implementations may be in-memory (for short-lived deployments
    or tests) or persistent (for multi-process deployments where tabs
    must survive restarts -- see refactor-plan §10.5).
    """

    async def save(self, tab: ConversationTab) -> None:
        """Insert or upsert ``tab`` (by id)."""
        ...

    async def get(self, tab_id: TabId) -> ConversationTab:
        """Return the tab; raise ``TabNotFoundError`` if missing."""
        ...

    async def find(self, tab_id: TabId) -> ConversationTab | None:
        """Return the tab or ``None``."""
        ...

    async def delete(self, tab_id: TabId) -> None:
        """Remove the tab record; raise ``TabNotFoundError`` if missing."""
        ...

    async def list_active(self) -> tuple[ConversationTab, ...]:
        """Return all tabs currently in non-terminal state."""
        ...


# ---------------------------------------------------------------------------
# Stream-abort registry
# ---------------------------------------------------------------------------
@runtime_checkable
class StreamAbortHandle(Protocol):
    """Cooperative cancellation handle for an in-flight stream.

    Implementations typically wrap an ``asyncio.Event`` plus a kill
    callback.  ``signal()`` is idempotent: calling it twice has the
    same effect as calling it once.
    """

    def signal(self, *, reason: str = "user_requested") -> None:
        """Request cancellation of the stream.  Must be idempotent."""
        ...

    def is_set(self) -> bool:
        """Return ``True`` iff cancellation has been requested."""
        ...

    def request_retry_now(self) -> None:
        """Ask a network-retry backoff to stop waiting and re-open now.

        Additive (AGENTS.md §3.1): an independent signal from :meth:`signal`
        — it does NOT abort the turn, it just cuts short the current
        network-retry backoff so the LLM stream re-opens immediately (used by
        the "立即重试" button after the user manually restores connectivity).
        Handles predating this method are probed via ``getattr`` by callers.
        """
        ...

    def consume_retry_now(self) -> bool:
        """Return ``True`` once (and clear) iff a retry-now was requested."""
        ...

    def request_cancel_tool(self, call_id: str) -> None:
        """Ask ONE in-flight tool call (by ``call_id``) to be cancelled.

        Additive (AGENTS.md §3.1): an independent, NON-terminal signal — like
        :meth:`request_retry_now` it does NOT abort the turn / trip
        :meth:`is_set`. It records ``call_id`` in a pending set so the tool
        dispatcher can cancel just that one running tool, synthesize a
        ``[cancelled]`` ``tool_result`` for it, and let the round keep draining
        the other tools and the turn continue to the next round. Handles
        predating this method are probed via ``getattr`` by callers.
        """
        ...

    def consume_cancel_tool(self, call_id: str) -> bool:
        """Return ``True`` once (and clear) iff ``call_id`` was cancel-requested.

        The dispatcher polls this per in-flight tool call; a ``True`` means the
        user asked to cancel THAT specific tool (as opposed to the whole turn).
        """
        ...


@dataclass(frozen=True, slots=True)
class ActiveStreamSnapshot:
    """Process-local snapshot of a registered chat stream abort handle."""

    tab_id: TabId
    started_at: datetime
    aborted: bool
    reason: str | None = None


@runtime_checkable
class StreamAbortRegistryPort(Protocol):
    """Registry of in-flight stream handles keyed by :class:`TabId`.

    The :class:`StreamChatUseCase` calls :meth:`register` when starting
    a new stream and :meth:`unregister` when the stream ends.
    :class:`StopChatUseCase` calls :meth:`abort` to signal a running
    stream to stop cooperatively.
    """

    def register(self, *, tab_id: TabId, handle: StreamAbortHandle) -> None:
        """Record a new in-flight stream handle.

        Raises :class:`ConversationLockedError` if a handle is already
        registered for the same tab.
        """
        ...

    def unregister(self, tab_id: TabId) -> None:
        """Drop the handle for ``tab_id``.  Idempotent."""
        ...

    def abort(self, tab_id: TabId, *, reason: str = "user_requested") -> bool:
        """Signal cancellation.

        Returns ``True`` iff a handle was found and signalled,
        ``False`` if no stream was registered for this tab.
        """
        ...

    def is_streaming(self, tab_id: TabId) -> bool:
        """Return ``True`` iff a handle is currently registered."""
        ...

    def is_aborted(self, tab_id: TabId) -> bool:
        """Return ``True`` iff a registered handle has been signalled.

        Additive method (AGENTS.md §3.1: append-only).  The stream handle
        stays *registered* for the whole turn, so :meth:`is_streaming` cannot
        tell "still streaming" from "aborted but not yet unwound" — the
        blocking ``question`` tool needs this distinction to stop waiting when
        the user presses stop.          Callers that hold a pre-``is_aborted`` registry
        stub should probe via ``getattr(registry, "is_aborted", None)``.
        """
        ...

    def request_retry_now(self, tab_id: TabId) -> bool:
        """Ask a tab's in-flight network-retry backoff to re-open now.

        Additive method (AGENTS.md §3.1: append-only).  Independent of
        :meth:`abort` — it does NOT tear the turn down; it merely cuts short
        the current escalating-backoff wait so the LLM stream re-opens
        immediately (the "立即重试" button after the user manually restores
        connectivity).  Returns ``True`` iff a handle was found and signalled.
        Callers holding a pre-``request_retry_now`` stub should probe via
        ``getattr(registry, "request_retry_now", None)``.
        """
        ...

    def cancel_tool(self, tab_id: TabId, call_id: str) -> bool:
        """Ask ONE in-flight tool call (``call_id``) on ``tab_id`` to cancel.

        Additive method (AGENTS.md §3.1: append-only).  Independent of
        :meth:`abort` — it does NOT tear the turn down; it records ``call_id``
        on the tab's handle so the dispatcher cancels just that one tool,
        synthesizes a ``[cancelled]`` ``tool_result`` for it, and lets the turn
        continue to the next round with the other tools' results.  Returns
        ``True`` iff a handle was found and the request was recorded.  Callers
        holding a pre-``cancel_tool`` stub should probe via
        ``getattr(registry, "cancel_tool", None)``.
        """
        ...

    def list_active(self) -> tuple[ActiveStreamSnapshot, ...]:
        """Return process-local active stream handles.

        The registry is the direct truth source for in-flight streams; persisted
        tab status can be stale after disconnects or process restarts.
        """
        ...


class InjectionRegistryPort(Protocol):
    """Registry of a tab's pending mid-turn user injections, keyed by TabId.

    A V2 enhancement (V1 has no equivalent).  While a turn streams the user may
    click the **inject** button to fold a new ``role:user`` instruction into the
    SAME run.  The route-layer control WebSocket records it via :meth:`inject`;
    the :class:`StreamChatUseCase` reads it via :meth:`drain` at its inter-round
    seam (between finishing one tool round and opening the next LLM round) and
    appends each pending injection as a ``role:user`` message.

    Distinct from the Enter-while-streaming *message queue* (a frontend-only
    construct that sends a NEW turn after the current one ends) and from
    ``question_registry`` (a model-initiated pause).  ``drain`` clears the
    pending set so each injection is folded in exactly once; ``clear`` is the
    turn-teardown cleanup so a never-drained injection does not leak forward.

    Image parity: an injection carries its image(s) the SAME way a normal
    submit does — as ``![](url)`` markdown inlined in the injection ``text`` —
    so this port needs no separate media field. The run loop extracts the refs
    from the text at the inter-round seam and resolves them to vision blocks.
    """

    def inject(self, tab_id: TabId, text: str) -> bool:
        """Record a pending injection for ``tab_id``.

        Returns ``True`` iff a non-empty injection was recorded (empty /
        whitespace-only text is rejected).
        """
        ...

    def drain(self, tab_id: TabId) -> list[str]:
        """Atomically return AND clear the tab's pending injections (FIFO).

        Returns an empty list when nothing is pending.
        """
        ...

    def has_pending(self, tab_id: TabId) -> bool:
        """Return ``True`` iff the tab has at least one pending injection."""
        ...

    def withdraw(self, tab_id: TabId, text: str) -> bool:
        """Remove the first pending injection matching ``text`` (FIFO).

        Called when the user edits/cancels a not-yet-folded injection bubble
        so the run loop does not also fold it in. Returns ``True`` iff one
        was removed; ``False`` is a benign no-op (already drained / not
        pending).
        """
        ...

    def clear(self, tab_id: TabId) -> None:
        """Drop the tab's pending injections (turn teardown).  Idempotent."""
        ...


# ---------------------------------------------------------------------------
# Agentic-loop ports (PR-401b / S7.5 lane L4)
#
# These four ports are the abstractions :class:`StreamChatUseCase` will need
# in PR-401c to host the legacy ``backend/chat_handler.py`` agentic loop
# behaviour (prompt-too-long retry, throttling backoff, per-tool-call
# guardrails, adaptive tool-result truncation, system-prompt assembly) in a
# clean-architecture friendly form.  PR-401b only **declares** the ports plus
# their value objects and provides default in-process adapters; PR-401c
# wires them into the use case.
# ---------------------------------------------------------------------------


# ----- GuardrailPort -----
class GuardrailDecision(str, Enum):
    """Verdict produced by :meth:`GuardrailPort.check`."""

    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"


@dataclass(frozen=True, slots=True, kw_only=True)
class GuardrailVerdict:
    """Outcome of a guardrail :meth:`GuardrailPort.check` invocation."""

    decision: GuardrailDecision
    reason: str = ""


@runtime_checkable
class GuardrailPort(Protocol):
    """Per-tool-call guardrail (anomaly detection + intervention).

    Migrated from ``backend/tool_guardrails.py:GuardrailController``.  The
    controller observes every tool invocation and looks at the trailing
    history to detect three abnormal patterns:

    * exact-argument repeats (warn at 2, block at 5);
    * consecutive failures of the same tool (warn at 3, block at 8);
    * idempotent tool repeatedly returning the same result (warn at 2).

    All implementations MUST be pure-in-memory and **session-scoped**: a
    fresh :class:`GuardrailPort` should be obtained per chat turn.  The
    chat use case owns the lifetime; it does not share controllers
    across tabs / conversations to avoid cross-talk.
    """

    def check(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> GuardrailVerdict:
        """Return the guardrail's verdict for the prospective call."""
        ...

    def observe(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        success: bool = True,
    ) -> None:
        """Record the outcome of a tool invocation."""
        ...

    def reset(self) -> None:
        """Forget all history.  Idempotent."""
        ...


# ----- HookEnginePort -----
@dataclass(frozen=True, slots=True, kw_only=True)
class HookFiredRecord:
    """One observation of a hook execution.

    Migrated from the ``ai_coding`` agent harness ``hook_engine.py``.
    ``error`` is ``None`` on success; otherwise a short reason
    (``"timeout"`` / ``"spawn_failed: ..."`` / ``"exit_code=N"``).

    Interceptor fields (V2 enhancement — mirrors the Claude-Agent-SDK
    ``PreToolUseHookSpecificOutput`` shape). When a ``pre_tool_call`` hook
    prints a JSON object on stdout, the engine parses it into these
    **optional appended** fields (AGENTS.md §3.1 — tail-only growth; a hook
    that prints nothing / non-JSON leaves them at their benign defaults so a
    plain logging hook is byte-for-byte unchanged):

    * ``decision`` — the hook's verdict for the prospective tool call
      (:class:`~qai.chat.domain.hook.HookDecision`); ``None`` means "the hook
      did not steer the call" (proceed as ``ALLOW``).
    * ``reason`` — human-readable justification surfaced to the model when
      the call is denied (``[hook_blocked] {reason}``).
    * ``updated_input`` — a replacement ``arguments`` dict the loop uses in
      place of the model's original tool arguments (e.g. a hook that
      normalises a path or injects a mandatory flag). ``None`` means
      "keep the model's arguments".
    * ``additional_context`` — extra text a ``pre_message`` /
      ``on_user_input`` hook wants folded into the turn so the model sees it
      (e.g. current git branch, lint summary). ``None`` / empty means
      "inject nothing".
    """

    event: HookEvent
    command: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    decision: "HookDecision | None" = None
    reason: str = ""
    updated_input: dict[str, Any] | None = None
    additional_context: str = ""


@runtime_checkable
class HookEnginePort(Protocol):
    """Run operator-configured shell commands at agent-loop lifecycle points.

    Migrated into the chat context from the (removed) ``ai_coding`` agent
    harness so the single wired chat agentic loop owns the capability.
    The chat use case calls :meth:`fire` at each :class:`HookEvent`
    point (session start/end, pre/post message, pre/post tool call,
    error, truncate); the engine looks up the configured
    :class:`HookConfig` for that event (if any) and executes it.

    Contract:

    * :meth:`fire` MUST NOT raise on hook failure by default — a buggy
      hook command logs + continues the turn (operator opt-in strict
      mode may raise);
    * a session with no hook for an event pays zero cost (``fire``
      returns ``None`` fast);
    * implementations are cheap to construct per turn.
    """

    def has_hook(self, event: HookEvent) -> bool:
        """Return ``True`` if a hook command is configured for ``event``."""
        ...

    async def fire(
        self,
        event: HookEvent,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> HookFiredRecord | None:
        """Run the configured hook for ``event``; ``None`` if none configured."""
        ...


# ----- SettingResolverPort -----
@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedSettings:
    """Outcome of :meth:`SettingResolverPort.resolve`.

    ``values`` is the merged mapping (read-only by convention).
    ``order`` records the applied source order (highest precedence
    first); ``missing`` lists requested source names that had no
    registered source.
    """

    values: Mapping[str, Any]
    order: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self.values


@runtime_checkable
class SettingResolverPort(Protocol):
    """Merge named setting overlays into one view (global + user).

    Aligns with the V1 ``forge_config.json`` deep-merge behaviour: V1
    has no per-project rule file, so the chat resolver layers only
    ``global`` (forge_config defaults) and ``user`` (user_prefs
    overrides).  Earlier names in ``order`` win.  The concrete source
    content is supplied by the apps layer (which may read
    ``qai.user_prefs`` via a bridge — chat never imports it directly).
    """

    def resolve(self, *, order: tuple[str, ...]) -> ResolvedSettings:
        """Merge the named sources into one mapping (left = highest precedence)."""
        ...


# ----- ToolResultTruncatorPort -----
@dataclass(frozen=True, slots=True, kw_only=True)
class ToolResultTruncationRequest:
    """Inputs to :meth:`ToolResultTruncatorPort.truncate`."""

    model_id: str
    tool_name: str
    result_text: str
    # Tail-appended (§3.1): signals that the producing tool ALREADY bounded
    # its own output (it self-reported ``truncated`` and/or persisted the full
    # body to the oversized-output store, embedding a recovery hint). When set
    # the adaptive truncator MUST pass the text through unchanged — a second
    # head+tail split here would corrupt the tool's own recovery footer and
    # drop the middle of a slice that is already recoverable upstream. Defaults
    # to ``False`` so callers that do not know the structured flag keep the
    # prior behaviour (the head+tail backstop still applies).
    already_truncated: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolResultTruncationResult:
    """Outputs of :meth:`ToolResultTruncatorPort.truncate`."""

    text: str
    truncated: bool
    original_length: int
    final_length: int
    omitted_chars: int = 0


@runtime_checkable
class ToolResultTruncatorPort(Protocol):
    """Compute an adaptive truncation of a tool result.

    Migrated from the ad-hoc adaptive truncation block at
    ``backend/chat_handler.py:820-854``.  The legacy logic looked at the
    model's family / context-length / current usage to pick a per-call
    budget and split the surplus into a head + tail summary.  Adapters
    are free to implement the budget heuristic differently as long as
    they honour the contract: ``result.text`` is safe to forward to the
    LLM and ``result.truncated`` accurately reports whether anything
    was elided.
    """

    def truncate(
        self,
        request: ToolResultTruncationRequest,
    ) -> ToolResultTruncationResult:
        """Apply (or skip) truncation."""
        ...


# ----- RetryPolicyPort -----
class RetryCategory(str, Enum):
    """Failure categories the chat use case may ask the policy about."""

    PROMPT_TOO_LONG = "prompt_too_long"
    THROTTLING = "throttling"
    NETWORK = "network"
    """A transient network failure reaching the model service — a connect
    error / read or write timeout / mid-stream socket drop that MAY take a
    while to self-heal (adapter codes ``chat.llm.connect_error`` /
    ``chat.llm.timeout`` / ``chat.llm.read_error``). Retries with a capped
    escalating backoff (``3s → 5s → 10s → 30s …``) but — unlike the previous
    behaviour — is now bounded by a WALL-CLOCK budget: once cumulative retry
    time exceeds :data:`~qai.chat.adapters.retry_policy.NETWORK_WALL_CLOCK_BUDGET_SECONDS`
    the policy returns ``should_retry=False`` and the turn surfaces the
    terminal error. The retry sleeps are abortable — a user Stop interrupts
    the wait immediately."""
    BOUNDED_FAST = "bounded_fast"
    """A connectivity fault that either recovers almost immediately or is
    effectively permanent: DNS resolution failure, connection refused, host
    unreachable (adapter codes ``chat.llm.dns_error`` /
    ``chat.llm.connection_refused`` / ``chat.llm.host_unreachable``). A few
    FAST attempts (``~1s → 3s → 10s``, max 3) then terminal — there is no
    point waiting minutes for a refused port or an unresolvable host."""
    BOUNDED_SERVER = "bounded_server"
    """An upstream server error (HTTP 5xx, adapter code
    ``chat.llm.server_error``). A few jittered attempts (``~2s → 5s → 15s``,
    max 3) then terminal."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryDecision:
    """Outcome of :meth:`RetryPolicyPort.next_attempt`.

    ``should_retry`` is the contract: when ``True``, the caller is
    expected to wait ``delay_seconds`` (already jittered if applicable)
    and re-issue the upstream call; when ``False``, the caller bubbles
    the original error.  ``compress_target_ratio`` is only meaningful
    for :data:`RetryCategory.PROMPT_TOO_LONG` and tells the caller how
    aggressively to compress before retrying (0 < ratio <= 1.0; 0.5
    matches the legacy ``force_compress(target_ratio=0.50)`` path).
    """

    should_retry: bool
    delay_seconds: float = 0.0
    compress_target_ratio: float | None = None
    attempt_number: int = 0


@runtime_checkable
class RetryPolicyPort(Protocol):
    """Retry policy for transient streaming errors.

    Maps directly onto the two retry branches at
    ``backend/chat_handler.py:476-540``:

    * **prompt_too_long** — single attempt; force-compress to the chosen
      ratio; do not delay.
    * **throttling** — up to 3 attempts; exponential backoff base 1.5s
      with ±20% jitter (``1.5 * 2**(attempt-1) * random(0.8, 1.2)``).
    * **network** — a transient connection failure to the model service.
      Retries **indefinitely** (``should_retry`` stays ``True``) with an
      escalating, capped backoff schedule ``3s → 5s → 10s → 30s → 30s …``
      so a flaky network self-heals without losing the turn. The caller
      sleeps ``delay_seconds`` between attempts and the sleep is abortable
      (a user Stop ends the wait at once).
    """

    def next_attempt(
        self,
        *,
        category: RetryCategory,
        attempt_number: int,
        server_advised_delay_s: float | None = None,
        elapsed_s: float | None = None,
    ) -> RetryDecision:
        """Decide whether and how to retry a failed attempt.

        ``attempt_number`` is the ordinal of the attempt that JUST
        failed (1-based).  Implementations return a decision that
        instructs the caller about the *next* attempt.

        ``server_advised_delay_s`` (additive, §3.1; default ``None`` keeps
        every existing caller byte-for-byte unchanged) carries a delay the
        upstream explicitly requested via a ``Retry-After`` header, already
        parsed into non-negative seconds and clamped. It is only consulted
        by the :data:`RetryCategory.THROTTLING` branch: when a finite,
        non-negative value is supplied, the policy honours it verbatim as
        ``delay_seconds`` (no jitter, no rng consumed); when ``None`` (absent
        / malformed / expired header) the branch falls back to its normal
        exponential-backoff-with-jitter schedule. Other categories ignore
        this hint.

        ``elapsed_s`` (additive, default ``None``) is the cumulative
        wall-clock time (seconds) the caller has already spent retrying this
        turn. It is consulted ONLY by :data:`RetryCategory.NETWORK` to
        enforce a wall-clock budget: once ``elapsed_s`` exceeds the policy's
        network budget the decision is ``should_retry=False`` (terminal) so a
        never-recovering network no longer retries forever. ``None`` keeps
        the pre-budget behaviour for callers that cannot measure elapsed
        time (they fall back to an attempt-count proxy inside the policy).
        """
        ...


# ----- SystemPromptBuilderPort -----
@dataclass(frozen=True, slots=True, kw_only=True)
class SystemPromptRequest:
    """Inputs to :meth:`SystemPromptBuilderPort.build`."""

    tool_mode: str | None = None
    """e.g. ``"model-build"`` / ``"app-builder"`` / ``"translate"``;
    ``None`` selects the default catalogue prompt."""
    tool_params: dict[str, Any] | None = None
    is_local_model: bool = False
    extra: dict[str, Any] | None = None
    skip_identity: bool = False
    """Discussion-mode opt-in (additive, §3.1): skip the QAI ModelBuilder
    identity intro + the ``_MODEL_BUILD_FALLBACK`` routing block in the
    default branch (those are about *who QAI is* + V1 routing fallbacks).
    Keep everything else (skill_rule / tools_intro / Python env / workspace
    context / Available Skills catalog / Few-shot / persona / suffix).

    Used by :class:`OrchestrateDiscussionUseCase` so each speaker sees the
    full SKILL / environment knowledge but NOT the QAI brand identity —
    speakers are user-defined roles, not QAI. ``False`` keeps V1 parity for
    ordinary single-assistant chat."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemPromptResult:
    """Return value of :meth:`SystemPromptBuilderPort.build`.

    Bundles the assembled system prompt text together with the
    *effective* tool mode that was used.  When auto-detection promotes
    a request (e.g. ``None`` → ``"model_build"``), the caller can
    compare ``effective_tool_mode`` against the original
    ``SystemPromptRequest.tool_mode`` to decide whether to emit a
    ``tool_mode_changed`` SSE frame.
    """

    prompt: str
    """Assembled system prompt text (may be empty)."""

    effective_tool_mode: str | None = None
    """The tool mode actually used by the builder.  Equals
    ``request.tool_mode`` when no auto-detection fired; otherwise
    reflects the detected mode (e.g. ``"model_build"``)."""


@runtime_checkable
class SystemPromptBuilderPort(Protocol):
    """Assemble the system prompt segment for a chat turn.

    Migrated from ``backend/chat_handler.py._build_cloud_system_prompt``
    (3285-3072) + ``_build_local_messages`` (952-1055).  Two adapters
    implement this port: :class:`StaticSystemPromptBuilder` returns a
    single configurable constant (used by tests and minimal deployments)
    and :class:`RichSystemPromptBuilder` materialises the full
    SKILL / persona / feature-mode catalogue for production wiring.
    The application root selects between them based on configuration.
    """

    def build(self, request: SystemPromptRequest) -> SystemPromptResult:
        """Return the system prompt text and effective tool mode."""
        ...


# ---------------------------------------------------------------------------
# Title generation + memory recall (PR-402 / S7.5 lane L4)
#
# Migrates :func:`backend.title_generator.generate_title` and
# :class:`backend.memory.ExperienceMemory` into the chat bounded context
# behind dedicated ports.  Title generation is a thin LLM call (one-shot,
# 30 max_tokens) so it gets its own port rather than re-using
# :class:`LLMStreamPort` (different timeout, different prompt template,
# different post-processing — quotation strip, length cap).  Memory recall
# is a query-side concern over the same ``chat_experience`` table
# :class:`ExperienceRepositoryPort` already manages, but with a free-text
# query interface (LIKE / future FTS5) and a context-block builder that
# materialises the legacy ``<past_experiences>`` XML block for prompt
# injection — both are read-only sides not appropriate for the
# CRUD-shaped :class:`ExperienceRepositoryPort`.
# ---------------------------------------------------------------------------


# ----- TitleGeneratorPort -----
@dataclass(frozen=True, slots=True, kw_only=True)
class TitleGenerationRequest:
    """Inputs to :meth:`TitleGeneratorPort.generate`."""

    user_message: str
    """The first user message of the conversation."""
    timeout_seconds: float = 10.0
    """Hard cap on the LLM round-trip; matches legacy
    ``backend/title_generator.py:generate_title(timeout=10.0)``."""
    model_id: str | None = None
    """The model the conversation is currently using (e.g. the cloud model
    id recorded on the latest assistant message). Lets a provider-aware
    :class:`TitleGeneratorPort` route the title request to the SAME cloud
    provider the user is chatting with (V1 parity: title summarisation ran
    against a cloud model, never the local on-device service). ``None`` =
    no hint → the adapter uses its configured default endpoint. Optional
    appended field (AGENTS.md §3.1)."""


@runtime_checkable
class TitleGeneratorPort(Protocol):
    """One-shot LLM call that synthesises a 3-7 word conversation title.

    Migrated from ``backend/title_generator.py:generate_title`` (98 LOC).
    Implementations must:

    * return ``None`` on any failure (timeout, network, malformed response,
      title shorter than 2 chars after cleaning) — callers fall back to
      :func:`fallback_title` heuristics;
    * never raise — title generation is **observability-grade** (legacy
      module-level docstring) and must not propagate exceptions to the
      chat use case;
    * strip surrounding quotes (ASCII " ' plus Unicode " " ' ');
    * truncate cleaned title to 50 characters max.
    """

    async def generate(
        self,
        request: TitleGenerationRequest,
    ) -> str | None:
        """Return the cleaned title or ``None`` on any failure."""
        ...


# ----- ExperienceRecallPort -----
@dataclass(frozen=True, slots=True, kw_only=True)
class ExperienceRecall:
    """Single :class:`ExperienceRecallPort.recall` hit.

    A thin projection of the underlying :class:`Experience` plus the
    matched ``id`` / ``category`` / ``content`` and the optional
    ``metadata`` dict.  Adapters MUST NOT leak DB rows; this dataclass
    is the canonical wire shape.
    """

    experience_id: str
    category: str
    content: str
    metadata: dict[str, Any]
    relevance: float = 0.0
    """``[0, 1]`` heuristic relevance score; defaults to ``0.0`` when the
    underlying matcher does not produce ranking information (e.g. LIKE
    fallback).  Higher is better."""


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryContextBlock:
    """Output of :meth:`ExperienceRecallPort.build_context_block`.

    ``text`` is the materialised ``<past_experiences>`` XML block
    intended to be appended to the system prompt; an empty ``text``
    means there were no relevant hits and the caller should NOT inject
    anything.  ``hit_ids`` echoes the experience ids that fed the
    block so observers can audit which knowledge was surfaced.
    """

    text: str
    hit_ids: tuple[str, ...]


@runtime_checkable
class ExperienceRecallPort(Protocol):
    """Query-side port for the experience knowledge base.

    Migrated from ``backend/memory.py:ExperienceMemory.{recall,
    build_context_block}`` (267 LOC).  Read-only and intentionally
    distinct from :class:`ExperienceRepositoryPort` (CRUD): the recall
    side wants free-text query + relevance ordering, which is awkward
    to retrofit onto the CRUD signature without breaking the v2.7 §3.1
    namespace lock.

    Implementations MUST be safe to call concurrently; no connection
    state may leak between calls.
    """

    async def recall(
        self,
        *,
        query: str,
        limit: int = 5,
    ) -> tuple[ExperienceRecall, ...]:
        """Return up to ``limit`` experiences matching ``query``."""
        ...

    async def build_context_block(
        self,
        *,
        query: str,
        max_chars: int = 3000,
    ) -> MemoryContextBlock:
        """Render a ``<past_experiences>`` XML block for prompt injection.

        Returns an empty :class:`MemoryContextBlock` (``text=""``,
        ``hit_ids=()``) when there are no relevant hits, so callers
        can safely concatenate the result without conditional logic.
        """
        ...


# ---------------------------------------------------------------------------
# Image upload + prompt enhance + prompt snapshot (PR-403 / S7.5 lane L4)
# ---------------------------------------------------------------------------


# ----- ImageUploadStorePort -----
@dataclass(frozen=True, slots=True, kw_only=True)
class ImageUploadRequest:
    """Inputs to :meth:`ImageUploadStorePort.save_base64`."""

    conversation_id: str
    """Soft conversation id used purely for directory grouping; the
    chat aggregate is NOT loaded by the upload store."""
    message_id: str
    """Soft message id used to make the saved filename unique."""
    base64_data: str
    """The image bytes, base64-encoded.  Must NOT include any
    ``data:...;base64,`` prefix."""
    mime_type: str
    """e.g. ``image/png`` / ``image/jpeg``.  Adapters map known types
    to canonical extensions; unknown types fall back to ``jpg``."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ImageUploadResult:
    """Outputs of :meth:`ImageUploadStorePort.save_base64`.

    ``url`` is a relative URL the front-end uses to fetch the image
    back via the static-files mount; ``disk_path`` is the absolute
    filesystem path so AppBuilder runners (and other downstream tools)
    can read the bytes without going through HTTP again.
    """

    url: str
    disk_path: str | None


@runtime_checkable
class ImageUploadStorePort(Protocol):
    """Persistence port for chat image uploads.

    Migrated from :class:`backend.image_store.ImageStore` (155 LOC).
    Adapters MUST be filesystem-safe: ``conversation_id`` /
    ``message_id`` are user-supplied and may carry path-traversal
    patterns; legitimate adapters sanitize them before composing the
    path.
    """

    async def save_base64(
        self,
        request: ImageUploadRequest,
    ) -> ImageUploadResult:
        """Decode and persist; return URL + on-disk path."""
        ...

    def get_path(self, url: str) -> Path | None:
        """Reverse a ``/api/images/files/...`` URL to its on-disk path.

        Returns ``None`` for URLs outside the image mount prefix or when the
        file does not exist (and on any path-traversal attempt). Synchronous
        introspection helper used by downstream consumers that already hold
        the URL and need the bytes (AppBuilder runners; the chat agentic loop
        decoding ``question``-answer images into vision blocks). Additive to
        the locked port surface — pre-existing adapters already implement it.
        """
        ...


# ----- PromptEnhancerPort -----
@dataclass(frozen=True, slots=True, kw_only=True)
class PromptEnhanceRequest:
    """Inputs to :meth:`PromptEnhancerPort.enhance`."""

    text: str
    """Raw user prompt text; must be non-empty and ``<= 8000`` chars
    (legacy guard)."""
    model_id: str | None = None
    model_provider: str | None = None
    timeout_seconds: float = 30.0


@runtime_checkable
class PromptEnhancerPort(Protocol):
    """One-shot LLM call that rewrites ``request.text`` into a higher-
    quality prompt.

    Migrated from ``backend/main.py:enhance_prompt`` + the
    ``_direct_chat_completion`` helper (~80 LOC combined).  Returns the
    enhanced text on success.  Returns ``None`` on any failure
    (timeout / network / empty response) so the route layer can return
    a 502 to the caller — this avoids leaking provider-specific error
    shapes through the chat surface.

    Implementations MUST NOT raise.
    """

    async def enhance(
        self,
        request: PromptEnhanceRequest,
    ) -> str | None:
        """Return the enhanced text or ``None`` on failure."""
        ...


# ----- PromptSnapshotStorePort -----
@dataclass(frozen=True, slots=True, kw_only=True)
class PromptSnapshot:
    """Single ``/api/prompt-snapshot/{request_id}`` result.

    Snapshots are debugging artefacts captured by the streaming use
    case; they let users inspect the exact ``messages`` list (system
    prompt + history + tool defs) sent to the LLM, plus the
    ``tool_mode`` and resolved ``model_id`` selected.  The snapshot is
    a frozen dict — adapters MUST NOT modify it after capture.
    """

    request_id: str
    payload: dict[str, Any]


@runtime_checkable
class PromptSnapshotStorePort(Protocol):
    """In-memory ring buffer of recent prompt snapshots.

    Migrated from the module-level ``backend/main.py:_prompt_snapshots``
    dict (line 1320-1376) — same eviction policy: at most
    ``capacity`` snapshots, FIFO eviction.  Adapters SHOULD be
    in-process (legacy was) — durability is not a goal; debug
    captures are short-lived.
    """

    async def save(self, snapshot: PromptSnapshot) -> None:
        """Insert a new snapshot; evicts the oldest when at capacity."""
        ...

    async def save_shared_prefix(
        self,
        *,
        request_id: str,
        turn_ref: str,
        shared_messages: list[dict[str, Any]],
        prefix_len: int,
        model_id: str,
        tool_mode: str,
        timestamp: str,
        request_options: dict[str, Any] | None = None,
    ) -> None:
        """Save one agentic round as a *prefix* of a shared turn message list.

        Storage-efficiency optimisation (O(N) instead of O(N²)): every
        round of one agentic *turn* sends a ``wire_messages`` list that is
        a strict *prefix* of the turn's final (longest) round, because each
        round only appends ``assistant{tool_calls}`` + ``role:tool`` blocks
        on top of the previous round.  Rather than deep-copying the full
        growing history once per round (N+1 copies, total ≈ N²/2 messages),
        the store keeps **one** shared list per ``turn_ref`` (the longest
        seen so far) and records each ``request_id`` as just a
        ``prefix_len`` boundary into it.

        :meth:`get` slices ``shared_messages[:prefix_len]`` to rebuild the
        exact per-round payload — so the returned :class:`PromptSnapshot`
        payload shape is **identical** to the legacy full-copy path
        (``{model_id, tool_mode, messages, timestamp}``); the front-end is
        unaware of the sharing.

        Implementations MUST:

        * reuse the same underlying list for repeated saves with the same
          ``turn_ref`` (a longer ``shared_messages`` *extends* / replaces
          the stored list; shorter saves keep the longer stored copy since
          shorter rounds remain valid prefixes of it);
        * count the FIFO ring-buffer capacity by ``request_id`` (one per
          round), and release a turn's shared list once its **last**
          referencing ``request_id`` is evicted (no leak);
        * truncate oversized ``role:tool`` content for debug display.

        ``request_options`` (additive, v2.7 §3.1) is the optional dict of
        non-message wire fields (resolved tools / tool_choice / sampling
        params / session_id) captured for the debug dialog; when provided it
        is attached to the rebuilt payload under a ``request_options`` key.
        """
        ...

    async def get(self, request_id: str) -> PromptSnapshot | None:
        """Return the snapshot or ``None`` if not found / evicted."""
        ...


# ---------------------------------------------------------------------------
# Context compression (A3 — three-level context compression)
# ---------------------------------------------------------------------------


@runtime_checkable
class ContextCompressionPort(Protocol):
    """Compress conversation messages to fit within token budget.

    Implementations apply a multi-level pipeline (tool-output pruning,
    LLM summarization, hard trim) to reduce the message list size while
    preserving the most recent ``preserve_tail`` messages intact.
    """

    async def compress(
        self,
        messages: list[dict[str, Any]],
        *,
        target_ratio: float = 0.5,
        preserve_tail: int = 4,
        budget_tokens: int | None = None,
        protect_ratio: float = 0.35,
        wire_actual_tokens: int | None = None,
        group_real_tokens: list[int] | None = None,
        target_window_ratio: float | None = None,
        model_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return compressed messages, keeping recent context intact.

        ``budget_tokens`` (optional): the model's context window in tokens.
        When supplied the compressor anchors its target to the WINDOW (compress
        down to ``budget × ~0.35``) and protects the most-recent
        ``protect_ratio`` of the window verbatim — instead of the legacy
        "compress to ``chars_before × target_ratio``" which is unrelated to the
        window. When ``None`` the legacy ratio-of-input behaviour is used
        (backwards compatible).

        ``wire_actual_tokens`` (optional): the provider-measured token size of
        the WHOLE ``messages`` wire (``total_tokens - completion_tokens``, the
        de-distorted real prompt). When supplied the compressor derives this
        conversation's real token density and converts char counts to tokens
        with it (far more accurate than a fixed chars/token factor, and calls
        no tokenizer). Falls back to a fixed factor when absent.

        ``group_real_tokens`` (optional, tail-appended per AGENTS.md §3.1):
        the "real-token differential" path
        (``docs/90-refactor/CONTEXT-COMPRESSION.md``). The list
        is parallel to the atomic groups of ``messages`` — same order, same
        length as ``_split_atomic_groups(messages)`` — each entry being the
        provider-measured (or single-round tokenizer-fallback)
        **per-group PROMPT-TOKEN INCREMENT** that atomic group adds to the
        wire. ⚠️ **NOT a cumulative running total** — Level 3 sums entries
        directly to compute "tokens released by dropping this group". Caller
        is responsible for differencing adjacent rounds / turns BEFORE passing
        the list (see ``build_group_real_tokens`` in ``_streaming_helpers.py``).

        Caller MUST construct it **same-source same-order** with the wire it
        passes as ``messages`` (no post-hoc reverse lookup from wire dicts
        back to source messages), so the alignment is by construction and
        immune to compressor-internal reorderings (Level 1 tool prune /
        Level 2 summarisation).

        Level 3 trim uses these per-group real-token contributions directly
        when supplied, falling back to char × density for groups whose entry
        is ``0`` (not measured). MUST NOT trigger any whole-wire tokenizer
        pass — that is a project-level performance red line.

        The most-recent ``protect_ratio`` of the window (plus the current turn)
        is preserved VERBATIM, so a single pass never collapses recent context.

        ``target_window_ratio`` (optional, tail-appended per AGENTS.md §3.1):
        the fraction of the model window the post-compression wire should be
        compressed down to. Only used when ``budget_tokens`` is known
        (window-anchored). When ``None`` (the default) the implementation uses
        its built-in default (0.35) — so omitting it reproduces the prior
        behaviour exactly. Exposed as the user-configurable "post-compression
        keep size" slider; clamped to ``[0.01, 1.0]`` by the implementation.

        ``model_hint`` (optional, tail-appended per AGENTS.md §3.1): the id of
        the conversation's CURRENT model (``request.model_hint`` —
        ``local::<id>`` for on-device, a cloud model id otherwise). It is
        threaded down to the Level 2 LLM summarisation call
        (``_generate_summary``) so the summary request resolves to the SAME
        live provider/endpoint the conversation is using, instead of a static
        ``"qai-default"`` that may resolve to no configured endpoint (which
        silently returns the offline placeholder and makes Level 2 fall back to
        Level 3 hard-trim). When ``None`` (the default) the prior static-default
        resolution is used (backwards compatible).
        """
        ...
# ---------------------------------------------------------------------------
# Model resolution (A4 feature)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """Result of model resolution.

    Captures the concrete LLM endpoint configuration after resolving
    a user's model selection (or falling back to the default).

    Migrated from the resolution block at
    ``backend/chat_handler.py:285-365``.
    """

    base_url: str
    api_key: str | None
    model_id: str
    is_local: bool = False
    #: Upstream *wire* model id sent to the provider's API when it differs
    #: from the display ``model_id`` (V1 ``api_model_id`` override, e.g.
    #: display ``claude-sonnet-4`` → wire ``claude-sonnet-4-20250514``).
    #: ``None`` means "no override" → callers send ``model_id`` verbatim.
    api_model_id: str | None = None
    #: Per-model sampling-parameter constraints declared in the cloud
    #: model catalog config (``cloud_models.json`` ``models[].params``).
    #: Shape: ``{"temperature": {"supported": bool, "default": number,
    #: "min": number, "max": number}, "top_p": {...}, "max_tokens": {...},
    #: "thought_signature": {"required": bool}}``.  ``None`` / empty means
    #: "no explicit constraint" → fall back to the family-regex defaults
    #: in :mod:`qai.chat.domain.model_profiles`.  The user configures these
    #: in Settings → Cloud Models so a model whose id was renamed (and thus
    #: no longer matches the hard-coded family regex) can still declare e.g.
    #: ``temperature.supported=false`` explicitly.
    params: dict[str, Any] | None = None


@runtime_checkable
class ModelResolverPort(Protocol):
    """Resolve which LLM endpoint to use for a chat request.

    The old code read ``ui.selected_model_id`` from forge config,
    resolved via a model registry to get the actual endpoint, detected
    ``is_local`` (model running locally vs cloud), and fell back to a
    running local model from ``list_models()``.

    Implementations may range from a simple settings-based lookup
    (single provider) to a full model-catalog-driven resolver with
    local/cloud routing.
    """

    async def resolve(self, hint: str | None = None) -> ResolvedModel:
        """Resolve model hint to concrete endpoint config.

        If hint is None, use the default/selected model.
        """
        ...


# ---------------------------------------------------------------------------
# Provider endpoint lookup (cross-context boundary; block 2 routing fix)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderEndpoint:
    """Concrete cloud-provider endpoint config for a single model.

    Returned by :class:`ProviderConfigLookupPort.lookup_for_model` when a
    model id belongs to a configured cloud provider (``cloud_llm`` /
    ``provider_b`` / ...).  Bundles the provider's ``base_url`` (from the
    ``model_catalog.provider.<id>`` config) with the provider's
    ``api_key`` (read from the OS-keyring-backed ``SecretStore``) so the
    chat resolver can route the request to the correct upstream.

    ``api_key`` is ``None`` when the provider has no stored credential
    (e.g. a keyless local gateway); callers MUST treat that as "send no
    bearer token" rather than an error.
    """

    provider_id: str
    base_url: str
    api_key: str | None = None
    #: Upstream *wire* model id when the provider config's model entry
    #: carries an ``api_model_id`` override distinct from its display
    #: ``model_id`` (V1 parity). ``None`` → send the requested ``model_id``
    #: verbatim.
    api_model_id: str | None = None
    #: Per-model sampling-parameter constraints from the provider config's
    #: model entry (``models[].params``).  Threaded through to
    #: :class:`ResolvedModel.params` so chat payload construction can honour
    #: ``temperature.supported=false`` etc.  ``None`` → no explicit
    #: constraint (family-regex defaults apply).
    params: dict[str, Any] | None = None


@runtime_checkable
class ProviderConfigLookupPort(Protocol):
    """Resolve a model id to its owning cloud provider's endpoint + key.

    Block-2 routing fix: the legacy
    :class:`~qai.chat.adapters.model_resolver.SettingsBasedModelResolver`
    returned the same ``base_url`` / ``api_key`` for every model id, so a
    user who selected a cloud model (``cloud_llm`` / ``provider_b`` / ...)
    still had their chat request sent to whatever single endpoint the chat
    settings happened to carry.  :class:`ProviderConfigLookupPort` lets the
    resolver ask "which provider owns this model, and what are its
    ``base_url`` + ``api_key``?" without the chat context ever importing
    ``qai.model_catalog`` or ``qai.platform`` directly.

    The concrete adapter lives at the ``apps/api`` composition root
    (:mod:`apps.api._model_resolver_bridge`) where reading the
    ``model_catalog`` provider registry and the platform ``SecretStore`` is
    legitimate; the chat context only ever sees this abstraction
    (preserves the ``context-isolation`` import-linter contract).

    Implementations MUST be safe to call concurrently and MUST NOT raise on
    a miss — they return ``None`` so the resolver can fall back to the
    settings-based default endpoint.
    """

    async def lookup_for_model(
        self, model_id: str
    ) -> ProviderEndpoint | None:
        """Return the endpoint owning ``model_id`` or ``None`` on a miss.

        ``None`` means: no configured cloud provider lists ``model_id`` in
        its catalog; the caller should fall back to the default endpoint.
        """
        ...


# ---------------------------------------------------------------------------
# Local on-device service endpoint (local-model routing fix)
# ---------------------------------------------------------------------------
#: Provider resolving the live base URL of the local on-device inference
#: service (GenieAPIService).  A zero-arg callable returning the base URL
#: string (``"http://127.0.0.1:9999/v1"``) — or an empty string / ``None``
#: when no endpoint can be determined.  May be sync or async; callers must
#: await the result when it is awaitable.
#:
#: V1 routed every on-device (``local::``) model turn to the running
#: GenieAPIService at ``http://{host}:{port}/v1`` where the port came from
#: ``forge_config.service_launch.local_port`` (default ``8910``) — see
#: ``backend/chat_handler.py:_stream_local`` (lines 1832-1842).  V2 split the
#: inference daemon into the ``model_runtime`` bounded context, so the chat
#: resolver cannot read the running port directly without violating the
#: ``context-isolation`` import-linter contract.  The concrete callable lives
#: at the ``apps/api`` composition root
#: (:mod:`apps.api._local_service_endpoint_bridge`) where reading
#: ``container.model_runtime.inference_service.status()`` and the
#: ``forge_config.service_launch.local_port`` fallback is legitimate.
#:
#: Implementations MUST NOT raise — they return an empty string / ``None``
#: when no local endpoint can be determined so the resolver falls back
#: cleanly to the offline notice.
LocalEndpointProviderPort = Callable[[], "str | None | Awaitable[str | None]"]


# ---------------------------------------------------------------------------
# App-builder Pack catalog (PR-091 / S9 audit H-4)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AppBuilderModelCode:
    """One selected App Builder model's inference-code *reference*.

    Chat-side, apps-layer-shaped DTO returned by
    :meth:`AppBuilderSkillCatalogPort.resolve_model_inference_code`. It
    carries only the reference ``runner.py`` *path* for one user-selected
    model so the chat system prompt can point the Agent at it — the Agent
    decides whether to ``read`` the full code, keeping the prompt small.
    The chat context never imports the App Builder ``ModelInferenceCode``
    dataclass — the adapter maps that into this plain, dependency-free
    shape at the ``apps/api`` boundary.
    """

    model_id: str
    title: str
    code_path: str


@runtime_checkable
class AppBuilderSkillCatalogPort(Protocol):
    """Cross-context port that resolves App Builder Pack metadata.

    The chat context never imports ``qai.app_builder.*`` directly
    (``import-linter`` ``context-isolation`` contract); the
    ``apps/api`` layer wires an adapter that bridges the call.
    See :mod:`apps.api._skill_registry_bridge`.

    Two distinct queries:

    * :meth:`resolve_skill_files` — return the list of SKILL.md
      filesystem paths to inject in the system prompt for the given
      ``tool_mode`` (typically ``"app-builder"``) and tool params
      (``{"selected_model_id": ...}``).  Returns an empty list when
      no SKILL is configured or the App Builder context is not wired.
    * :meth:`generate_pack_catalog_prompt` — return a Markdown block
      summarising every Pack the App Builder context knows about.
      Returns the empty string when no Packs are registered.

    Both methods are async because adapters may need to read from the
    Pack registry which lives behind an aiosqlite connection.
    """

    async def resolve_skill_files(
        self,
        *,
        tool_mode: str,
        tool_params: dict[str, Any] | None,
    ) -> tuple[str, ...]:
        """Return the paths of SKILL files to inline in the system prompt."""
        ...

    async def generate_pack_catalog_prompt(self) -> str:
        """Return a Markdown summary of every registered Pack (or "")."""
        ...

    async def resolve_model_inference_code(
        self,
        *,
        tool_params: dict[str, Any] | None,
    ) -> tuple["AppBuilderModelCode", ...]:
        """Return the inference code blocks for the selected model(s).

        For each model id in ``tool_params`` (``selected_model_id`` and/or
        ``selected_model_ids``, unioned + deduped preserving order), return
        one :class:`AppBuilderModelCode` carrying its (possibly truncated)
        ``runner.py`` reference code so the App Builder chat system prompt
        can inline it. Returns an empty tuple when no model is selected,
        none has a readable runner, or the App Builder context is not
        wired. Never raises.
        """
        ...


# ---------------------------------------------------------------------------
# Runtime-learned API limit store (D2 — model_profiles domain purification)
# ---------------------------------------------------------------------------
@runtime_checkable
class RuntimeLimitStorePort(Protocol):
    """Mutable cache of per-model API limits learned at runtime.

    The legacy ``backend/model_profiles.py`` held a module-level
    ``_runtime_limits`` dict + ``threading.Lock`` inside the domain
    layer so that, after the upstream API rejected an oversized
    ``max_tokens`` with a ``"expected a value <= N"`` style 400, the
    learned cap ``N`` was cached and applied on the next request.

    That mutable global + lock violate domain purity (the domain layer
    must hold no process-global mutable state, no threading primitives,
    no I/O).  D2 lifts the *state* up into this application port; the
    domain keeps only the pure extraction rule
    (:func:`qai.chat.domain.model_profiles.extract_api_limit_from_error`)
    and the pure clamp logic (``ModelProfile.resolve_max_tokens`` now
    takes the learned cap as a plain argument).

    The concrete adapter
    (:class:`qai.chat.adapters.InMemoryRuntimeLimitStore`) owns the dict
    + lock and is wired as a process singleton in ``apps/api/_chat_di``.
    Implementations MUST be safe to call concurrently.
    """

    def record_limit(self, *, model_id: str, max_tokens_max: int) -> None:
        """Cache a learned ``max_tokens`` ceiling for ``model_id``."""
        ...

    def get_limit(self, *, model_id: str, key: str) -> int | None:
        """Return a previously learned limit value (or ``None``)."""
        ...

    def clear(self, *, model_id: str | None = None) -> None:
        """Drop all learned limits, or just those for ``model_id``."""
        ...


# ---------------------------------------------------------------------------
# Code-persona resolver (R12 — cross-BC persona lookup via apps bridge)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ResolvedCodePersona:
    """A resolved code persona (prompt + display name + tool groups).

    Apps-layer-shaped DTO returned by :class:`CodePersonaResolverPort`.
    ``prompt`` is the (override-applied) working-role system prompt;
    ``name`` is the human-readable display name (may be ``None`` when
    the persona record carried no name);
    ``groups`` is the (override-applied) list of tool-group identifiers
    that this persona is permitted to use (e.g. ``["read", "edit",
    "command"]``).  ``None`` means "all groups" (no restriction).
    """

    prompt: str
    name: str | None = None
    groups: tuple[Any, ...] | None = None


@runtime_checkable
class CodePersonaResolverPort(Protocol):
    """Cross-context port that resolves a code persona by id.

    The legacy ``chat_handler.code_persona_manager`` looked a selected
    persona id up against the ``user_prefs`` ``ui.code_personas`` prefs
    document and applied per-persona overrides.  In V2 the chat context
    may NOT import ``qai.user_prefs.*`` (``import-linter``
    ``context-isolation`` contract); the resolution is therefore wired
    at the ``apps/api`` composition root
    (:class:`apps.api._code_persona_bridge.CodePersonaResolverBridge`)
    where reading the ``user_prefs`` document use case + the pure
    ``CodePersonaManager`` domain helper is legitimate.

    :meth:`resolve` returns ``None`` when the id is unknown or the
    persona carries no usable prompt; callers then leave the system
    prompt unchanged.  The method is async because the adapter reads
    the prefs document behind an aiosqlite connection.
    """

    async def resolve(
        self, persona_id: str, locale: str | None = None
    ) -> ResolvedCodePersona | None:
        """Return the resolved persona for ``persona_id`` (or ``None``)."""
        ...


# ---------------------------------------------------------------------------
# MCP (Model Context Protocol) server registry
# ---------------------------------------------------------------------------
# The chat context can connect to external MCP servers (Anthropic open
# standard) that expose extra TOOLS.  A connected server's tools are advertised
# to the LLM alongside the built-in tools and, when the model calls one, the
# invocation flows back through the SAME ``ToolInvocationPort`` pipeline the
# built-in tools use — the concrete registry adapter registers each MCP tool as
# a handler + schema on the shared ``RegistryBackedToolInvocation`` so
# advertise-filtering / guardrails / truncation auto-cover them (no
# streaming-use-case change needed).
#
# Secure-by-default: the concrete registry is DISABLED unless a ``Settings``
# flag (``chat.chat_mcp_enabled``) is truthy — analogous to the
# ``SubprocessHookEngine`` ``enabled=False`` gate — because an MCP stdio server
# is an arbitrary local subprocess.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class McpServerStatus:
    """Live status projection of one registered MCP server.

    Returned by :meth:`McpServerRegistryPort.list_servers`.  ``connected`` is
    the truth-source of whether the server is currently reachable (State-Truth-
    First — it reflects the last real connect/discover attempt, not an
    optimistic flag).  ``tool_count`` is the number of tools discovered on the
    last successful connect; ``error`` carries a short human-readable reason
    when ``connected`` is ``False`` (empty otherwise).
    """

    config: "McpServerConfig"
    connected: bool
    tool_count: int = 0
    tool_names: tuple[str, ...] = ()
    error: str = ""


@runtime_checkable
class McpServerRegistryPort(Protocol):
    """Manage the set of external MCP servers + their advertised tools.

    Implementations own the connection lifecycle (spawn stdio subprocess /
    open sse-http session), discover each server's tools, and register those
    tools onto the shared chat :class:`ToolInvocationPort` so they are
    advertised + invoked through the SAME pipeline as the built-in tools.

    All methods MUST be safe to call concurrently and MUST NOT raise on a
    predictable failure (unknown name, connect error) — :meth:`test_server`
    returns a status carrying the error; the mutation methods surface failures
    through the returned / subsequently-listed :class:`McpServerStatus`.

    Secure-by-default: when the registry is disabled (the ``chat_mcp_enabled``
    Settings gate is ``False``) :meth:`list_servers` still returns the persisted
    configs (so the UI can show + edit them), but :meth:`add_server` /
    :meth:`test_server` never spawn a subprocess / open a network session —
    they return a status with ``connected=False`` and an explanatory ``error``.
    """

    async def list_servers(self) -> tuple[McpServerStatus, ...]:
        """Return every registered server with its live connection status."""
        ...

    async def add_server(self, config: "McpServerConfig") -> McpServerStatus:
        """Register (or replace, by ``name``) a server, connect + discover.

        Persists the config, then — when the registry is enabled — connects,
        discovers the server's tools, and registers each onto the shared tool
        port.  Returns the resulting :class:`McpServerStatus` (``connected`` +
        ``error`` reflect the real connect attempt).  Replacing an existing
        server by the same ``name`` first unregisters its previously-registered
        tools.
        """
        ...

    async def remove_server(self, name: str) -> bool:
        """Unregister the server ``name`` and drop its tools.

        Returns ``True`` iff a server with that name existed.  Idempotent — a
        missing name is a benign ``False``.  Also removes any SecretStore
        credentials persisted for that server's headers.
        """
        ...

    async def test_server(self, name: str) -> McpServerStatus:
        """Connect to the already-registered server ``name`` and re-discover.

        A read-only probe used by the UI's "Test connection" button: it
        (re)connects, lists tools, and returns the fresh status WITHOUT
        changing the persisted config.  Returns a ``connected=False`` status
        with an ``error`` when the name is unknown or the connect fails.
        """
        ...

    async def aclose(self) -> None:
        """Close all open sessions / terminate all spawned subprocesses.

        Called from the application ``lifespan`` shutdown.  Idempotent.
        """
        ...

    # --- resources / prompts surface (tail-appended — MCP-RESOURCES-SURFACE) ---
    # These four methods extend the registry to the MCP resources + prompts
    # surface. They obey the SAME secure gate as the tools surface: when the
    # registry is disabled (``chat_mcp_enabled=False``) they return empty /
    # raise "mcp disabled"; per-server they only query a server that is
    # CURRENTLY connected. An unconnected / failed / removed server is skipped,
    # so NO resource / prompt of an un-enabled or unreachable server is ever
    # surfaced to the model (user mandate).

    async def list_resources(self) -> tuple["McpResource", ...]:
        """Aggregate the resources of every CONNECTED server (``resources/list``).

        Returns an empty tuple when the registry is disabled. Only servers whose
        last connect succeeded (``connected``) are queried; a server that fails
        to respond is skipped (its previously-known resources are dropped), never
        raised. The order is server-registration order, resources within a server
        in the server's advertised order.
        """
        ...

    async def list_prompts(self) -> tuple["McpPrompt", ...]:
        """Aggregate the prompts of every CONNECTED server (``prompts/list``).

        Same gate + skip semantics as :meth:`list_resources`.
        """
        ...

    async def read_resource(self, server_name: str, uri: str) -> str:
        """Read one resource (``resources/read``) from a CONNECTED server.

        Returns the resource's consolidated text. Raises / returns an error
        string when the registry is disabled, the server is unknown / not
        connected, or the read fails — the caller (the exposed tool handler)
        surfaces that as a normal tool error so the model can react.
        """
        ...

    async def get_prompt(
        self, server_name: str, name: str, arguments: dict[str, Any]
    ) -> str:
        """Render one named prompt (``prompts/get``) from a CONNECTED server.

        Returns the rendered prompt text. Same disabled / unknown / not-connected
        error semantics as :meth:`read_resource`.
        """
        ...

    # --- marketplace: per-server switch + curated catalog (tail-appended —
    # MCP marketplace phase 1; AGENTS.md §3.1 append-only) ---

    async def set_enabled(self, name: str, enabled: bool) -> "McpServerStatus":
        """Flip one server's per-server ``enabled`` switch, persist, apply.

        A server is only surfaced to the model when the GLOBAL gate
        (``chat_mcp_enabled``) AND this per-server ``enabled`` AND the current
        connection state are ALL true (three-way AND). Turning a server ON
        (re)connects + registers its tools/resources/prompts; turning it OFF
        disconnects + unregisters them (as thoroughly as the global gate) while
        KEEPING the persisted config so the user can re-enable later. Returns
        the resulting :class:`McpServerStatus`; a ``connected=False`` status
        with an ``error`` when the name is unknown.
        """
        ...

    async def list_catalog(self) -> tuple["CuratedCatalogEntry", ...]:
        """Return the curated marketplace catalog (the built-in 'curated' source).

        Phase-1 marketplace: a static, self-maintained list of high-frequency,
        credential-free, ``npx`` / ``uvx`` (packages + stdio) MCP servers the
        user can browse + install. The entry carries a command/args TEMPLATE
        (with ``<PLACEHOLDER>`` slots the user fills for e.g. a filesystem path)
        that :meth:`add_server` materialises into an :class:`McpServerConfig`.
        The shape is multi-source ready (each entry tags its ``source``) so a
        future dynamic registry source (phase 2) can be aggregated alongside the
        curated one. Independent of the enabled gate — browsing the catalog is
        always allowed; only connecting an installed server obeys the gate.
        """
        ...

    # --- marketplace phase 2: dynamic official-registry source (tail-appended —
    # MCP-MARKETPLACE-REGISTRY-P2; AGENTS.md §3.1 append-only) ---

    async def refresh_catalog(self) -> tuple["CuratedCatalogEntry", ...]:
        """Fetch the dynamic official-registry source ON DEMAND, return catalog.

        Phase-2: :meth:`list_catalog` returns the static ``curated`` source plus
        whatever dynamic ``registry`` entries are already cached, but NEVER
        auto-fetches the network. This method is the ONLY one that reaches the
        network — invoked when the user explicitly picks the "registry" source
        and clicks "load / refresh" (their click IS the consent to reach out;
        there is no hidden operator flag). Forces a fresh fetch (bypasses the
        TTL) and merges into the cache. Graceful on failure (degrades to curated
        + any prior cache, never raises). Implementations MAY default this to
        :meth:`list_catalog` when they have no dynamic source.
        """
        ...

    async def install_from_catalog(
        self,
        entry_id: str,
        *,
        name: str | None = None,
        arg_values: dict[str, str] | None = None,
        env_values: dict[str, str] | None = None,
        header_values: dict[str, str] | None = None,
        source: str | None = None,
    ) -> "McpServerStatus":
        """Materialise a catalog entry (curated OR registry) into a server.

        Resolves the entry across BOTH the curated and the (cached) dynamic
        registry source, builds the right :class:`McpServerConfig` for its
        transport — phase 1: ``stdio`` + ``<PLACEHOLDER>`` arg substitution;
        phase 2: also remote ``sse`` / ``http`` (``url`` + ``header_values``)
        and ``stdio`` + required ``env_values`` keys — then connects + persists
        via the same path as :meth:`add_server` (so the three-way gate +
        SecretStore externalisation apply). ``source`` (``"curated"`` /
        ``"registry"``) disambiguates a cross-source id collision so the user
        installs the exact card they clicked (``None`` = curated-wins fallback).
        Secret HEADER values (remote) and secret ENV values (stdio) are both
        externalised to the SecretStore (only a ``__secret__`` sentinel on disk;
        re-hydrated at load / injected into the child at spawn) — never persisted
        plain-text. Raises an install
        error (``ValueError`` subclass → HTTP 400) on an unknown entry / missing
        required argument / env / header.
        """
        ...

    # --- marketplace phase 3: global switch + registry browse (tail-appended —
    # MCP-MARKETPLACE-P3; AGENTS.md §3.1 append-only) ---

    def global_enabled(self) -> bool:
        """Return the GLOBAL master switch state (the single truth source).

        The global switch is layer 1 of the three-way gate (global AND
        per-server ``enabled`` AND connected). Seeded from the deployment
        default (``chat_mcp_enabled``) but thereafter persisted with the server
        configs and user-controllable via :meth:`set_global_enabled` — so the
        UI reads the LIVE truth here rather than the static Settings flag.
        """
        ...

    async def set_global_enabled(self, enabled: bool) -> None:
        """Flip the GLOBAL master switch, persist it, and re-apply every server.

        Turning it ON (re)connects + registers every per-server-enabled server;
        turning it OFF disconnects + unregisters them all (as thoroughly as the
        per-server gate) while KEEPING the persisted configs so it can be flipped
        back on. State-Truth-First: the per-server ``connected`` / ``error`` are
        driven by the real connect attempt (no optimistic write). The new state
        is persisted (single truth source) so it survives a restart.
        """
        ...

    async def browse_registry(
        self,
        *,
        search: str | None = None,
        cursor: str | None = None,
        limit: int = 30,
    ) -> tuple[tuple["CuratedCatalogEntry", ...], str | None]:
        """Browse ONE page of the dynamic official-registry source (user-driven).

        The search / pagination entry point for the marketplace (distinct from
        :meth:`list_catalog` and :meth:`refresh_catalog`). Reaches the network —
        the user's search / "load more" click IS the consent — fetching the page
        for the given ``search`` (server-side ``name`` substring filter) and
        ``cursor`` (opaque ``metadata.nextCursor`` from a prior page). Returns
        ``(entries, next_cursor)`` where ``next_cursor`` is ``None`` on the last
        page. Graceful: a fetch failure records the reason and returns
        ``((), None)`` — it NEVER raises. Browsed entries are merged into the
        install cache so a subsequent ``install_from_catalog(source="registry")``
        can resolve a browsed-but-not-first-page entry without a re-fetch.
        """
        ...


# ---------------------------------------------------------------------------
# Per-conversation token-budget tracker (max_budget_tokens feature)
#
# ▼▼▼ APPENDED BLOCK — keep additions HERE at the very end of the port
# definitions so a concurrent tail-append by another agent (e.g. the MCP
# agent) is unlikely to collide (AGENTS.md §6.2). ▼▼▼
# ---------------------------------------------------------------------------
@runtime_checkable
class BudgetTrackerPort(Protocol):
    """Per-conversation TOKEN-budget observer + enforcement gate.

    A conversation may carry an optional token cap (``max_budget_tokens``,
    renamed from the CC SDK's ``max_budget_usd`` — this project has no
    cross-provider USD pricing but DOES have accurate provider usage counts).
    The chat agentic loop (main + sub-agents) folds each round's
    PROVIDER-AUTHORITATIVE token delta into this tracker via :meth:`observe`
    and gates the START of each new round via :meth:`check`.

    Ownership / persistence
    -----------------------
    The reference adapter
    (:class:`~qai.chat.adapters.budget_tracker.ConversationBackedBudgetTracker`)
    keeps the running counter in :attr:`Conversation.meta` ``["budget"]``
    (``{"max_tokens": int|None, "used_tokens": int}``) via the
    :class:`ConversationRepositoryPort`, so no cross-context coupling to
    ``Conversation`` from the sub-agent handler is needed (it holds only THIS
    port). A :class:`~qai.chat.adapters.budget_tracker.NullBudgetTracker`
    no-op implementation (always ``exceeded=False``) is the default so callers
    that do not wire a real tracker see byte-for-byte unchanged behaviour.

    State-Truth-First (AGENTS.md 铁律 1)
    ------------------------------------
    :meth:`observe` MUST be fed only real, provider-measured deltas — a round
    with no measurement (local model / no usage) contributes NOTHING and is
    never blocked. The tracker NEVER estimates to enforce.
    """

    async def observe(
        self,
        conversation_id: ConversationId,
        delta_tokens: int,
    ) -> None:
        """Fold a round's provider-measured token delta into the counter.

        ``delta_tokens`` is ``effective_prompt_tokens_this_round +
        completion_tokens_this_round`` for the round that just completed. A
        non-positive delta (a round with no authoritative measurement) is a
        no-op — the counter never grows on an estimate and never regresses.
        Persists the updated counter (the reference adapter writes it back to
        ``Conversation.meta``). Best-effort: a missing conversation / write
        hiccup is swallowed so a bookkeeping failure never breaks a turn.
        """
        ...

    async def check(
        self,
        conversation_id: ConversationId,
    ) -> BudgetCheckResult:
        """Return the conversation's current budget snapshot.

        ``exceeded`` is ``True`` iff a cap is set AND ``used >= max_tokens``.
        When no cap is set (the default) ``exceeded`` is ALWAYS ``False``. A
        missing conversation / read hiccup degrades to a disabled result
        (``max_tokens=None`` ⇒ never blocks) — a bookkeeping failure must not
        strand a turn.
        """
        ...

    async def reset(self, conversation_id: ConversationId) -> None:
        """Zero the conversation's ``used_tokens`` counter (keep the cap).

        Idempotent; a missing conversation is a benign no-op. Used when the
        user changes the cap and wants a fresh accounting window.
        """
        ...

    async def set_max_tokens(
        self,
        conversation_id: ConversationId,
        max_tokens: int | None,
    ) -> BudgetCheckResult:
        """Set (or clear) the conversation's cap; return the fresh snapshot.

        Route-layer config entry point (``PATCH /conversations/{id}/budget``).
        ``max_tokens=None`` (or a non-positive value) DISABLES the budget;
        preserves the running ``used_tokens`` counter (use :meth:`reset` to zero
        it). The reference adapter persists the cap into
        ``Conversation.meta["budget"]`` and raises ``ConversationNotFoundError``
        (→ 404) when the conversation is missing; the no-op adapter returns a
        disabled result without persisting.
        """
        ...


@dataclass(frozen=True, slots=True)
class PromoteReadyVariant:
    """One promote-eligible precision variant found under a model workspace.

    Mirrors the App Builder ``BinScanResult`` shape the promote card consumes,
    but is a chat-context DTO so ``qai.chat`` never imports ``qai.app_builder``
    (the apps-layer adapter maps ``BinScanResult`` → this).
    """

    precision: str
    label: str


class PromoteReadyScanPort(Protocol):
    """Scan a model workspace directory for promote-eligible NPU weight
    variants (``output/<model>_<label>.{bin,dlc}``).

    Turn-end promote-ready detection
    (``StreamChatUseCase._finalize_assistant_message``) extracts the model
    workspace path from the turn's final summary text and asks this port
    whether that directory holds promotable precision variants. The result is
    persisted onto ``Conversation.detected_model`` so the frontend CTA needs
    ZERO on-open disk scans.

    Cross-context isolation (AGENTS.md / import-linter ``context-isolation``)
    ------------------------------------------------------------------------
    The real capability lives in ``qai.app_builder`` (``ImportScanBinsUseCase``),
    which ``qai.chat`` MUST NOT import. The apps/api composition root supplies
    an adapter (``_promote_ready_scan_bridge``) that maps this port onto that
    use case — exactly like ``_workspace_grant_bridge`` bridges chat → security.
    The chat context only ever names THIS port.

    State-Truth-First (AGENTS.md 铁律 1/3)
    -------------------------------------
    The returned variants reflect the REAL on-disk scan (``scanBins``), the
    same truth source the actual promote flow uses — no drift. An empty tuple
    means "scanned, nothing promotable"; the caller distinguishes that from
    "not scanned" (never called / error).
    """

    async def scan(self, model_workdir: str) -> tuple[PromoteReadyVariant, ...]:
        """Return the promote-eligible precision variants under ``model_workdir``.

        ``model_workdir`` is a top-level model workspace (e.g.
        ``C:\\WoS_AI\\resnet50``); the adapter scans ``<model_workdir>/output/``.
        Returns an empty tuple when the directory has no promotable variants.
        Best-effort: implementations SHOULD NOT raise on a missing directory /
        transient scan error (return an empty tuple instead) so a detection
        pass never breaks the turn.
        """
        ...


__all__ = [
    # repositories
    "ConversationRepositoryPort",
    "ExperienceRepositoryPort",
    "ConversationListItem",
    "MessagesPage",
    # llm + tools
    "LLMStreamPort",
    "LLMStreamRequest",
    "ToolInvocationPort",
    "ToolInvocationRequest",
    "ToolInvocationResult",
    "ToolStreamChunk",
    "ToolStreamChunkKind",
    "SubAgentEventStreamPort",
    # tab / abort
    "TabSessionStorePort",
    "StreamAbortRegistryPort",
    "StreamAbortHandle",
    # PR-401b — agentic-loop ports
    "GuardrailPort",
    "GuardrailDecision",
    "GuardrailVerdict",
    "HookEnginePort",
    "HookFiredRecord",
    "SettingResolverPort",
    "ResolvedSettings",
    "ToolResultTruncatorPort",
    "ToolResultTruncationRequest",
    "ToolResultTruncationResult",
    "RetryPolicyPort",
    "RetryCategory",
    "RetryDecision",
    "SystemPromptBuilderPort",
    "SystemPromptRequest",
    "SystemPromptResult",
    # PR-402 — title generation + memory recall
    "TitleGeneratorPort",
    "TitleGenerationRequest",
    "ExperienceRecallPort",
    "ExperienceRecall",
    "MemoryContextBlock",
    # PR-403 — image upload + prompt enhance + prompt snapshot
    "ImageUploadStorePort",
    "ImageUploadRequest",
    "ImageUploadResult",
    "PromptEnhancerPort",
    "PromptEnhanceRequest",
    "PromptSnapshotStorePort",
    "PromptSnapshot",
    # A3 — context compression
    "ContextCompressionPort",
    # A4 — model resolution
    "ModelResolverPort",
    "ResolvedModel",
    # block 2 — provider-aware routing
    "ProviderConfigLookupPort",
    "ProviderEndpoint",
    # local-model routing — on-device service endpoint
    "LocalEndpointProviderPort",
    # PR-091 — app-builder pack catalog
    "AppBuilderSkillCatalogPort",
    "AppBuilderModelCode",
    # D2 — runtime-learned API limit store
    "RuntimeLimitStorePort",
    # R12 — code-persona resolver (cross-BC via apps bridge)
    "CodePersonaResolverPort",
    "ResolvedCodePersona",
    # mid-turn user injection (V2 enhancement): inject button → inter-round seam
    "InjectionRegistryPort",
    # MCP (Model Context Protocol) server registry
    "McpServerRegistryPort",
    "McpServerStatus",
    # per-conversation token-budget tracker (max_budget_tokens feature)
    "BudgetTrackerPort",
    "BudgetCheckResult",
    # promote-ready detection scan (cross-BC via apps bridge → app_builder)
    "PromoteReadyScanPort",
    "PromoteReadyVariant",
]
