# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DI wiring for the ``chat`` bounded context (PR-033 / S3 + PR-042 + PR-043 / S4 + PR-401b + PR-402 / S7.5).

PR-033 (S3) injected seven ``_Fake<Port>`` in-memory adapters here;
PR-042 retired six of them (real SQLite / HTTP adapters), and PR-043
(S4) retires the final ``_FakeTabSessionStore`` and adds three
missing tab-management use cases.

PR-401b (S7.5 lane L4) **appends** four new fields to
:class:`ChatServices` (in strict accordance with the v2.7 §3.1
namespace lock — existing field names are never renamed; only
appending is permitted):

* ``guardrail_factory`` — ``Callable[[], GuardrailPort]`` producing a
  fresh per-turn :class:`InMemoryGuardrailController`.  Per-turn
  scope mirrors the legacy ``GuardrailController()`` instantiation.
* ``tool_result_truncator`` — singleton stateless
  :class:`AdaptiveToolResultTruncator`.
* ``retry_policy`` — singleton :class:`DefaultStreamRetryPolicy` for
  prompt-too-long + throttling retries.
* ``system_prompt_builder`` — singleton
  :class:`StaticSystemPromptBuilder`; PR-401c may swap for a richer
  strategy.

PR-402 (S7.5 lane L4) **appends** two more fields to
:class:`ChatServices` for title generation:

* ``title_generator`` — singleton :class:`TitleGeneratorPort` adapter
  (production: :class:`HttpLLMTitleGenerator`; offline:
  :class:`OfflineTitleGenerator` when the chat settings carry no
  upstream URL).
* ``generate_title_use_case`` — :class:`GenerateTitleUseCase`
  (LLM with deterministic fallback heuristic).

Real adapters wired here (PR-042 + PR-043 + PR-401b + PR-402):

* :class:`qai.chat.adapters.SqliteConversationRepository`
* :class:`qai.chat.adapters.SqliteTabSessionStore`             (PR-043)
* :class:`qai.chat.infrastructure.HttpOpenAICompatibleLLMStream`
* :class:`qai.chat.adapters.RegistryBackedToolInvocation`
* :class:`qai.chat.adapters.InMemoryStreamAbortRegistry`
* :class:`qai.chat.adapters.InMemoryGuardrailController`       (PR-401b; per-turn)
* :class:`qai.chat.adapters.AdaptiveToolResultTruncator`       (PR-401b; singleton)
* :class:`qai.chat.adapters.DefaultStreamRetryPolicy`          (PR-401b; singleton)
* :class:`qai.chat.adapters.StaticSystemPromptBuilder`         (PR-401b; singleton)
* :class:`qai.chat.adapters.HttpLLMTitleGenerator` /
  :class:`qai.chat.adapters.OfflineTitleGenerator`             (PR-402; selected by config)

Existing :class:`ChatServices` field names are part of the public route
contract (PR-033 manifest §11 lock) and have NOT been changed.  PR-402
merely appends.

Import discipline
-----------------
This module imports from ``qai.chat.adapters`` /
``qai.chat.infrastructure`` directly; the import-linter contract
``interfaces-stays-thin`` allows this since ``apps/api/_<context>_di.py``
is the canonical location for adapter wiring.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qai.chat.adapters import (
    IMPLEMENTATION_PLAN_TOOL_SCHEMA,
    QUESTION_TOOL_SCHEMA,
    RECALL_EXPERIENCE_TOOL_SCHEMA,
    REMEMBER_EXPERIENCE_TOOL_SCHEMA,
    SEARCH_CONVERSATIONS_TOOL_SCHEMA,
    SKILL_TOOL_SCHEMA,
    TODOWRITE_TOOL_SCHEMA,
    AdaptiveToolResultTruncator,
    AgentToolHandler,
    DefaultStreamRetryPolicy,
    FileSystemImageUploadStore,
    HttpLLMTitleGenerator,
    HttpPromptEnhancer,
    ImplementationPlanToolHandler,
    InMemoryGuardrailController,
    InMemoryInjectionRegistry,
    InMemoryPromptSnapshotStore,
    InMemoryQuestionRegistry,
    InMemoryRuntimeLimitStore,
    InMemoryStreamAbortRegistry,
    InMemorySubAgentAbortRegistry,
    LlmFeatureItemExtractor,
    LlmIntentClassifier,
    LocalModelStreamAdapter,
    McpServerRegistry,
    OfflinePromptEnhancer,
    OfflineTitleGenerator,
    ProviderAwareModelResolver,
    QueuedSubAgentSessionRepository,
    QuestionToolHandler,
    RecallExperienceToolHandler,
    RegistryBackedToolInvocation,
    RememberExperienceToolHandler,
    RichSystemPromptBuilder,
    SearchConversationsToolHandler,
    SkillToolHandler,
    SqliteAgentTemplateRepository,
    SqliteCompactionCheckpointRepository,
    SqliteConversationRepository,
    SqliteConversationSearchRepository,
    SqliteExperienceRecall,
    SqliteExperienceRepository,
    SqliteModeTemplateRepository,
    SqliteParticipantRepository,
    SqliteRosterTemplateRepository,
    SqliteSubAgentSessionRepository,
    SqliteTabSessionStore,
    ThreeLevelContextCompressor,
    TodoWriteToolHandler,
    ordered_slice_cap_for,
)
from qai.chat.adapters.budget_tracker import (
    ConversationBackedBudgetTracker,
    NullBudgetTracker,
)
from qai.chat.adapters.compaction_journal import CompactionJournal
from qai.chat.adapters.async_job_manager import AsyncJobManager
from qai.chat.adapters.injection_signal_bridge import InjectionSignalBridge
from qai.chat.adapters.subagent_bridge import SubAgentBridge
from qai.chat.adapters.agent_session_coordinator import (
    AgentSessionCoordinator,
)
from qai.chat.adapters.background_job_dispatcher import (
    BackgroundJobDispatcher,
)
from qai.chat.adapters.conversation_write_queue import (
    ConversationWriteQueue,
)
from qai.chat.adapters.scheduled_task_bridge import ScheduledTaskAsideBridge
from qai.chat.adapters.scheduled_task_runner import ScheduledTaskRunner
from qai.chat.adapters.scheduled_task_tool import (
    SCHEDULED_TASK_TOOL_SCHEMA,
    ScheduledTaskToolHandler,
)
from qai.platform.scheduling import SchedulerService, SqliteScheduledTaskStore
from apps.api._chat_sync_push import push_scheduled_task_to_channels
from qai.chat.application.chat_stream_broadcaster import ChatStreamBroadcaster
from qai.chat.application.ports import (
    BudgetTrackerPort,
    CompactionCheckpointStorePort,
    ContextCompressionPort,
    ConversationRepositoryPort,
    GuardrailPort,
    ImageUploadStorePort,
    LLMStreamPort,
    McpServerRegistryPort,
    ModelResolverPort,
    ParticipantRepositoryPort,
    PromptEnhancerPort,
    PromptSnapshotStorePort,
    RetryPolicyPort,
    RuntimeLimitStorePort,
    StreamAbortRegistryPort,
    SubAgentSessionRepositoryPort,
    SystemPromptBuilderPort,
    TabSessionStorePort,
    TitleGeneratorPort,
    ToolInvocationPort,
    ToolResultTruncatorPort,
)
from qai.chat.application.provider_cache_capability import (
    ProviderCacheCapabilityRegistry,
)
from qai.chat.application.sub_agent_stream_broadcaster import (
    SubAgentStreamBroadcaster,
)
from qai.chat.application.use_cases.agent_template_management import (
    ApplyAgentTemplateUseCase,
    CloneAgentTemplateUseCase,
    CreateAgentTemplateUseCase,
    DeleteAgentTemplateUseCase,
    ListAgentTemplatesUseCase,
    ResetAgentTemplateUseCase,
    UpdateAgentTemplateUseCase,
)
from qai.chat.application.use_cases.compact import GetContextDecisionUseCase
from qai.chat.application.use_cases.conversation_management import (
    CreateConversationUseCase,
    DeleteConversationUseCase,
    GetConversationMessagesUseCase,
    ListConversationsUseCase,
    RenameConversationUseCase,
    SetConversationFavoriteUseCase,
    SetConversationPinnedUseCase,
    SetConversationWorkspaceUseCase,
)
from qai.chat.application.use_cases.extras import (
    EnhancePromptUseCase,
    GetPromptSnapshotUseCase,
    SavePromptSnapshotUseCase,
    UploadImageUseCase,
)
from qai.chat.application.use_cases.force_compact import (
    ForceCompactChatUseCase,
)
from qai.chat.application.use_cases.fork_conversation import (
    ForkConversationUseCase,
)
from qai.chat.application.use_cases.implementation_planner import (
    OrchestratePlanningUseCase,
)
from qai.chat.application.use_cases.manage_mcp_servers import (
    ManageMcpServersUseCase,
)
from qai.chat.application.use_cases.migrate_conversation import (
    MigrateConversationUseCase,
)
from qai.chat.application.use_cases.mode_template_management import (
    ApplyModeTemplateUseCase,
    CloneModeTemplateUseCase,
    CountModeTemplateUsageUseCase,
    CreateModeTemplateUseCase,
    DeleteModeTemplateUseCase,
    ListModeTemplatesUseCase,
    ResetModeTemplateUseCase,
    UpdateModeTemplateUseCase,
)
from qai.chat.application.use_cases.orchestrate_discussion import (
    OrchestrateDiscussionUseCase,
)
from qai.chat.application.use_cases.participant_management import (
    CreateParticipantUseCase,
    DeleteParticipantUseCase,
    GenerateImplementationPlanUseCase,
    GetDiscussionConfigUseCase,
    GetImplementationPlanUseCase,
    ListParticipantsUseCase,
    SetDiscussionConfigUseCase,
    UpdateImplementationPlanUseCase,
    UpdateParticipantUseCase,
)
from qai.chat.application.use_cases.refresh_digest import (
    PROMPT_KEY_COMPACTION_SUMMARY,
    PROMPT_KEY_COMPACTION_TURN_PREFIX,
    PROMPT_KEY_COMPACTION_UPDATE_SUMMARY,
    PROMPT_KEY_SUMMARIZATION_SYSTEM,
    RefreshDigestUseCase,
)
from qai.chat.application.use_cases.roster_template_management import (
    ApplyRosterTemplateUseCase,
    CloneRosterTemplateUseCase,
    CreateRosterTemplateUseCase,
    DeleteRosterTemplateUseCase,
    ListRosterTemplatesUseCase,
    ResetRosterTemplateUseCase,
    UpdateRosterTemplateUseCase,
)
from qai.chat.application.use_cases.streaming import (
    AsyncioStreamAbortHandle,
    CancelToolUseCase,
    StopChatUseCase,
    StreamChatUseCase,
)
from qai.chat.application.use_cases.summarize_turn_prefix import (
    SummarizeTurnPrefixUseCase,
)
from qai.chat.application.use_cases.tab_management import (
    CloseTabUseCase,
    ListActiveTabsUseCase,
    OpenTabUseCase,
)
from qai.chat.application.use_cases.title import GenerateTitleUseCase
from qai.chat.infrastructure import (
    HttpOpenAICompatibleLLMStream,
    LazyReloadHookEngine,
    ProviderRoutingLLMStream,
    build_hook_engine,
)
from qai.platform.scheduling.path_locks import PathLockManager
from qai.platform.scheduling.tool_concurrency import ToolConcurrencyManager
from qai.platform.logging import get_logger

from .openai_compat_adapter import DefaultOpenAIModelListerAdapter
from .openai_compat_ports import OpenAIModelListerPort

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# ChatServices namespace
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChatServices:
    """Application services + ports for the ``chat`` bounded context.

    The dataclass field names are the public route contract (PR-033
    manifest §11 lock).  PR-042 / PR-043 / PR-401b only **add** fields;
    no existing field has been renamed.
    """

    # use cases (PR-033)
    create_conversation_use_case: CreateConversationUseCase
    list_conversations_use_case: ListConversationsUseCase
    get_conversation_messages_use_case: GetConversationMessagesUseCase
    rename_conversation_use_case: RenameConversationUseCase
    delete_conversation_use_case: DeleteConversationUseCase
    context_decision_use_case: GetContextDecisionUseCase
    stream_chat_use_case: StreamChatUseCase
    stop_chat_use_case: StopChatUseCase
    cancel_tool_use_case: CancelToolUseCase
    # direct ports (read-only paths the route layer composes itself)
    conversations: ConversationRepositoryPort
    tabs: TabSessionStorePort
    abort_registry: StreamAbortRegistryPort
    llm: LLMStreamPort
    tools: ToolInvocationPort
    # NEW (PR-042 / issue b): apps-layer port for /v1/models
    openai_model_lister: OpenAIModelListerPort
    # NEW (PR-043 / issue d): tab-management use cases
    open_tab_use_case: OpenTabUseCase
    close_tab_use_case: CloseTabUseCase
    list_active_tabs_use_case: ListActiveTabsUseCase
    # NEW (PR-401b / S7.5 lane L4): agentic-loop ports.  ``guardrail_factory``
    # produces a fresh per-turn controller so two parallel turns do not
    # share a history buffer; the other three are stateless singletons.
    guardrail_factory: Callable[[], GuardrailPort]
    tool_result_truncator: ToolResultTruncatorPort
    retry_policy: RetryPolicyPort
    system_prompt_builder: SystemPromptBuilderPort
    # NEW (PR-402 / S7.5 lane L4): title generation.
    title_generator: TitleGeneratorPort
    generate_title_use_case: GenerateTitleUseCase
    # NEW (PR-403 / S7.5 lane L4): image upload + prompt enhance + prompt snapshot.
    image_upload_store: ImageUploadStorePort
    prompt_enhancer: PromptEnhancerPort
    prompt_snapshot_store: PromptSnapshotStorePort
    upload_image_use_case: UploadImageUseCase
    enhance_prompt_use_case: EnhancePromptUseCase
    get_prompt_snapshot_use_case: GetPromptSnapshotUseCase
    save_prompt_snapshot_use_case: SavePromptSnapshotUseCase
    # NEW (A3): context compression port.
    context_compressor: ContextCompressionPort
    # NEW (A4): model resolution port.  Singleton; the resolver's output
    # is used at request time to determine which model/endpoint to call.
    model_resolver: ModelResolverPort
    # NEW (D2 dealign): runtime-learned API-limit store.  Singleton owning
    # the mutable ``{model_id: {max_tokens_max}}`` cache + lock that used
    # to live as a module-level global inside the chat *domain*
    # (``qai.chat.domain.model_profiles``).  Lifting it behind this port
    # keeps the domain pure (no process-global mutable state / threading
    # primitives) while preserving the V1 "learn the API ceiling from a
    # 400 once, clamp subsequent requests" behaviour.
    runtime_limit_store: RuntimeLimitStorePort
    # NEW (harness tools): registry pairing a tab's pending blocking
    # ``question`` with the user's answer; shared with the route-layer answer
    # endpoint so a submitted answer resolves the awaited future.
    question_registry: InMemoryQuestionRegistry
    # NEW (mid-turn injection feature): registry holding a tab's pending
    # user injections submitted via the inject button while a turn streams.
    # Shared with the route-layer control WS (writes via ``inject``) and the
    # streaming run loop (drains at its inter-round seam). Tail-appended
    # (AGENTS.md §3.1). Unlike ``question_registry`` (model-initiated pause),
    # this is USER-initiated mid-run steering folded into the same run.
    injection_registry: InMemoryInjectionRegistry
    # NEW (workspace feature): per-session workspace setter. Persists the
    # conversation's working directory into ``meta.workspace`` so the agentic
    # loop defaults the file/exec tools' root to it (→ global workspace when
    # unset). Tail-appended (AGENTS.md §3.1).
    set_conversation_workspace_use_case: SetConversationWorkspaceUseCase
    # NEW (pin / favorite feature): persist conversation pin & favorite flags
    # into ``meta.pinned`` / ``meta.favorite`` (no new column / migration;
    # same range as the workspace setter). The sidebar surfaces pinned
    # conversations on top; favorited ones appear in the favorites dialog.
    # Tail-appended (AGENTS.md §3.1).
    set_conversation_pinned_use_case: SetConversationPinnedUseCase
    set_conversation_favorite_use_case: SetConversationFavoriteUseCase
    # NEW (max_budget_tokens feature): per-conversation TOKEN-budget tracker.
    # Shared by the streaming use case + the sub-agent handler + the discussion
    # orchestrator (one budget pool per conversation — main agent, sub-/grand-
    # agents and discussion speakers all observe the same root_conversation_id
    # pool) and used by the route layer to read / set the cap
    # (``PATCH /conversations/{id}/budget``). A ``NullBudgetTracker`` when
    # ``Settings.chat_budget_enabled`` is False so enforcement/counting is off;
    # the flag defaults to True (budgeting active). Tail-appended (§3.1).
    budget_tracker: BudgetTrackerPort
    # NEW (sub-agent persistence / multi-agent groundwork): direct ports for
    # the sub-agent session store + participant registry. Tail-appended
    # (AGENTS.md §3.1 — namespace fields may only be appended). The route
    # layer composes the list/fetch/interrupt endpoints itself from these.
    subagent_sessions: SubAgentSessionRepositoryPort
    participants: ParticipantRepositoryPort
    # NEW (sub-agent live-stream, block 2): process-wide broadcaster that
    # fans out a running sub-agent's events to standalone sub-agent tabs
    # (``GET /api/chat/subagents/{id}/stream``). Tail-appended (§3.1). Held
    # as a singleton so the sub-agent loop's publisher and the SSE
    # subscribers share the same in-process frame buffers; ``lifespan``
    # calls ``aclose()`` on shutdown.
    subagent_stream_broadcaster: SubAgentStreamBroadcaster
    # NEW (sub-agent independent interrupt, block 3): per-sub-agent
    # cancellation registry keyed by sub-agent id. Tail-appended (§3.1). The
    # interrupt route signals it so a standalone sub-agent tab's "stop" stops
    # ONLY that sub-agent (cooperatively), not the parent tab's whole stream.
    subagent_abort_registry: InMemorySubAgentAbortRegistry
    # NEW (multi-agent discussion, block 4): the discussion orchestrator +
    # participant-roster / discussion-config management use cases. Tail-appended
    # (§3.1 — namespace fields may only be appended). Multi-agent discussion is
    # a pure V2 enhancement (细则 4-bis); these sit ALONGSIDE the single-agent
    # use cases and never touch their paths (non-discussion behaviour unchanged).
    #
    # ``orchestrate_discussion`` runs the OUTER speaker-selection loop (it reuses
    # the SAME shared kernel collaborators — compressor + truncator —
    # the main / sub-agent loops use). The SSE route dispatches to it instead of
    # ``stream_chat_use_case`` only when the conversation's discussion config or
    # the ``discussion`` query flag opts in. The four CRUD use cases + the two
    # discussion-config use cases back the participant / discussion REST
    # endpoints (thin handlers; business logic in application).
    orchestrate_discussion: OrchestrateDiscussionUseCase
    list_participants_use_case: ListParticipantsUseCase
    create_participant_use_case: CreateParticipantUseCase
    update_participant_use_case: UpdateParticipantUseCase
    delete_participant_use_case: DeleteParticipantUseCase
    get_discussion_config_use_case: GetDiscussionConfigUseCase
    set_discussion_config_use_case: SetDiscussionConfigUseCase
    # Implementation-plan read + edit use cases (DISC-1 二期-step4, §22.9). Back
    # the GET/PATCH ``…/implementation`` panel endpoints (thin handlers; plan
    # CRUD business logic in application). Tail-appended (§3.1). PURE V2
    # enhancement — read the plan stored at ``meta["discussion"]["implementation"]``
    # and let the user re-shape the item list (add/delete/edit/skip) while the
    # run state machine retains exclusive ownership of every execution-truth
    # field. Never touch single-agent paths.
    get_implementation_plan_use_case: GetImplementationPlanUseCase
    update_implementation_plan_use_case: UpdateImplementationPlanUseCase
    # Plan-generation use case (DISC-1 二期-step2, §22.4). Drives LLM extraction
    # of feature items from a converged discussion and persists the plan blob.
    # Tail-appended (§3.1). Never touches single-agent paths.
    generate_implementation_plan_use_case: GenerateImplementationPlanUseCase
    # Roster-template library use cases. Tail-appended (§3.1). PURE V2
    # enhancement (细则 4-bis): a named, reusable bundle of discussion role
    # definitions a user can preview + import into any conversation, so a
    # roster need not be rebuilt every time. These sit ALONGSIDE the
    # participant / discussion use cases and never touch single-agent paths.
    list_roster_templates_use_case: ListRosterTemplatesUseCase
    create_roster_template_use_case: CreateRosterTemplateUseCase
    update_roster_template_use_case: UpdateRosterTemplateUseCase
    delete_roster_template_use_case: DeleteRosterTemplateUseCase
    apply_roster_template_use_case: ApplyRosterTemplateUseCase
    # Clone + reset for roster templates. Tail-appended (§3.1). Clone copies any
    # template (incl. a read-only built-in preset) into a new is_builtin=0 user
    # record (cloned_from_id=source.id) — the "edit a preset" flow. Reset
    # restores a cloned copy's business fields from its source in place.
    clone_roster_template_use_case: CloneRosterTemplateUseCase
    reset_roster_template_use_case: ResetRosterTemplateUseCase
    # Agent-template library use cases (single-role; three-tier template system
    # §27). Tail-appended (§3.1). PURE V2 enhancement (细则 4-bis): a named,
    # reusable definition of a SINGLE discussion role a user can preview +
    # import into any conversation, the smallest reusable unit alongside the
    # roster (team) templates. Never touch single-agent paths.
    list_agent_templates_use_case: ListAgentTemplatesUseCase
    create_agent_template_use_case: CreateAgentTemplateUseCase
    update_agent_template_use_case: UpdateAgentTemplateUseCase
    delete_agent_template_use_case: DeleteAgentTemplateUseCase
    apply_agent_template_use_case: ApplyAgentTemplateUseCase
    # Clone + reset for agent templates. Tail-appended (§3.1). Same semantics as
    # the roster clone/reset pair (clone any template incl. preset into a new
    # user copy; reset a copy's fields from its source in place).
    clone_agent_template_use_case: CloneAgentTemplateUseCase
    reset_agent_template_use_case: ResetAgentTemplateUseCase
    # Mode-template library use cases (collaboration modes; three-tier template
    # system §26/§27). Tail-appended (§3.1). PURE V2 enhancement: a named mode
    # (识别/framing/工具策略/流程/安全) defining HOW a team collaborates, orthogonal
    # to the team (roster) templates. Selecting a mode is the user-explicit
    # privilege action (§26.4); never touches single-agent paths.
    list_mode_templates_use_case: ListModeTemplatesUseCase
    create_mode_template_use_case: CreateModeTemplateUseCase
    update_mode_template_use_case: UpdateModeTemplateUseCase
    delete_mode_template_use_case: DeleteModeTemplateUseCase
    apply_mode_template_use_case: ApplyModeTemplateUseCase
    count_mode_template_usage_use_case: CountModeTemplateUsageUseCase
    # Clone + reset for mode templates. Tail-appended (§3.1). Same semantics as
    # the roster/agent clone/reset pairs (clone any mode incl. preset into a new
    # user copy; reset a copy's fields from its source in place).
    clone_mode_template_use_case: CloneModeTemplateUseCase
    reset_mode_template_use_case: ResetModeTemplateUseCase
    # PARALLEL-TOOL-1: the process-lifetime shared per-path file lock (built in
    # ``build_chat_services``). Tail-appended (§3.1) with a default so the
    # post-build ``wire_ai_coding_tools_into_chat`` hook can reuse the SAME
    # instance when it (authoritatively) re-registers the ai_coding tools with
    # real adapters — otherwise the two registrations would use separate lock
    # tables and concurrent writers across them would still collide.
    path_lock: PathLockManager | None = field(default=None)
    # NEW (active-runs): ordinary chat stream fan-out/replay broadcaster.
    # Tail-appended (§3.1) so late-opened active-run tabs can attach to an
    # in-flight ordinary chat turn without stealing the primary WS/SSE stream.
    chat_stream_broadcaster: ChatStreamBroadcaster | None = field(default=None)
    # NEW (MCP integration): the Model Context Protocol server registry +
    # its thin management use case. Tail-appended (§3.1) with defaults so
    # existing constructions / minimal-container tests stay valid. The registry
    # owns MCP server config persistence + connection lifecycle and registers a
    # connected server's tools onto the SHARED ``tools`` port, so MCP tools flow
    # through the SAME advertise/guardrail/truncator pipeline as built-in tools
    # (no streaming-use-case change). Secure-by-default: disabled unless the
    # ``chat.chat_mcp_enabled`` Settings gate is truthy.
    mcp_server_registry: McpServerRegistryPort | None = field(default=None)
    manage_mcp_servers_use_case: ManageMcpServersUseCase | None = field(
        default=None
    )
    # NEW (conversation fork): fork a conversation (copy with message slicing).
    # Tail-appended (§3.1). Creates a new conversation with a subset of messages
    # from the source; lineage tracked via meta["parent_id"].
    fork_conversation_use_case: ForkConversationUseCase | None = field(
        default=None
    )
    # NEW (P5 handoff — CONTEXT-COMPRESSION-NEXT v3.1 §7.2): create a fresh
    # conversation seeded with the source's P2 session digest + reference
    # ledger when a single turn keeps re-tripping the compaction threshold.
    # Tail-appended optional field (§3.1) so callers that never enabled the
    # escape hatch (e.g. legacy channel bridges, unit-test compositions)
    # keep working unchanged with ``None``.
    migrate_conversation_use_case: MigrateConversationUseCase | None = field(
        default=None
    )
    # NEW (Task J — /compact refactor): force-compress on demand + expose the
    # checkpoint store directly so the ``/compact clear`` command can drop
    # the persisted digest / ledger / anchor. Tail-appended optional fields
    # (§3.1) — callers that never wired them keep working with ``None``.
    force_compact_chat_use_case: ForceCompactChatUseCase | None = field(
        default=None
    )
    compaction_checkpoint_store: CompactionCheckpointStorePort | None = field(
        default=None
    )
    # NEW: the live ``computer.enabled`` reader (forge_config override >
    # ``settings.computer.enabled``), exposed so the route layer can advertise
    # the SAME tool set the streaming loop will actually honour.
    #
    # ``computer`` is registered UNCONDITIONALLY (so the Settings toggle takes
    # effect per-turn with no restart) and dropped per-turn by
    # ``StreamChatUseCase._computer_enabled``. But ``GET /api/chat/tools``
    # enumerated the raw registry, so every front-end surface fed by it (the
    # per-session tool popover, the Discussion role allowlist) listed
    # ``computer`` even while it was disabled — offering the user a desktop
    # -control tool the model could never receive. Sharing the reader keeps the
    # advertised set and the enforced set identical. ``None`` → treated as
    # disabled (secure default), matching the streaming gate.
    computer_enabled: Callable[[], bool] | None = field(default=None)
    # NEW (scheduled-task feature): the runtime-dynamic scheduler backing the
    # ``scheduled_task`` tool, plus its completion bridge. Tail-appended
    # (§3.1). Both are started / stopped by the application lifespan (the
    # scheduler's tick loop and the bridge's event-bus subscription both need
    # a running loop, which ``build_chat_services`` — a sync factory — does
    # not have). ``None`` when the feature could not be constructed.
    scheduling_scheduler: "SchedulerService | None" = field(default=None)
    scheduling_aside_bridge: "ScheduledTaskAsideBridge | None" = field(default=None)
    # NEW (WebUI manager): the store + tool handler behind the ``scheduled_task``
    # tool, exposed so the ``/api/scheduled-tasks`` REST routes can list tasks
    # (store) and reuse the handler's validated create/update/pause/resume/
    # remove/run logic (single source of truth — the route never re-implements
    # schedule parsing / whitelist coercion). ``None`` when the feature is dark.
    scheduling_task_store: "SqliteScheduledTaskStore | None" = field(default=None)
    scheduling_tool_handler: "ScheduledTaskToolHandler | None" = field(default=None)
    # NEW (§6.4 refactor): unified background-job-completion pipeline —
    # the coordinator owns "does tab X have pending work" state, the
    # dispatcher translates sub-agent + background-exec completions into
    # SYSTEM_NOTICE messages + coordinator notifications.  Tail-appended
    # (§3.1).  ``None`` when the container is built without the events
    # bus (unit-test compositions); production DI wires both.  Lifespan
    # calls ``dispatcher.start()`` after this factory returns and
    # ``.stop()`` on shutdown.
    agent_session_coordinator: "AgentSessionCoordinator | None" = field(default=None)
    background_job_dispatcher: "BackgroundJobDispatcher | None" = field(default=None)
    # NEW (async-job registry): process-wide registry + admission cap for
    # asynchronous work (spawned sub-agents, backgrounded bash).  Tail-appended
    # (§3.1) with a default so minimal-container test compositions that build
    # ``ChatServices`` by hand stay valid — every field from ``path_lock``
    # (line 489) onward carries one, and a no-default field cannot follow a
    # defaulted one in a dataclass.  Production DI always passes a real
    # instance.  The lifespan starts ``dead_letter_evict_loop()`` at startup
    # (this factory is synchronous — no running loop here) and cancels it on
    # shutdown.
    async_job_manager: "AsyncJobManager | None" = field(default=None)
    # NEW (Batch 3 — unified async spawn path): the ONE way a sub-agent is
    # spawned.  Tail-appended (§3.1) with a default for the same reason as
    # ``async_job_manager`` above.  Exposed on the namespace because the
    # interrupt REST route cancels a single sub-agent through it (job
    # transition + per-sub-agent abort flag together — the bare abort registry
    # cannot release the admission slot).
    sub_agent_bridge: "SubAgentBridge | None" = field(default=None)
    # NEW (Batch 5 — exec 4-way race): fans this process's mid-turn
    # injection events out to the per-tab ``asyncio.Event`` a running
    # ``exec`` races on, so a Ctrl+Enter injection backgrounds a long
    # command IMMEDIATELY instead of waiting out the auto-background
    # threshold.  Tail-appended (§3.1) with a default for the same reason
    # as ``async_job_manager`` above.  Observes the SAME
    # ``injection_registry`` singleton the control WebSocket writes to.
    injection_signal_bridge: "InjectionSignalBridge | None" = field(
        default=None
    )
    # NEW (Batch 9 — concurrent-write safety): the per-conversation write
    # serialiser every ``chat_message`` writer during a turn goes through
    # (streaming saves + the background dispatcher's SYSTEM_NOTICE append).
    # Tail-appended (§3.1) with a default for the same reason as
    # ``async_job_manager`` above.  Exposed on the namespace because
    # ``lifespan.on_shutdown`` must ``close()`` it to drain the queues.
    conversation_write_queue: "ConversationWriteQueue | None" = field(
        default=None
    )


def _load_chat_hooks(container: Container) -> Any:
    """Build the chat hook engine from ``<data>/config/forge_config.json``.

    Reads the optional ``chat.hooks`` array (each entry
    ``{"event": "...", "command": "...", "timeout_s": 30}``) and returns
    a :class:`qai.chat.application.ports.HookEnginePort`.  Returns a
    no-op engine when the file / section is absent or malformed — hooks
    are an opt-in operator feature and must never break chat startup.

    V1 parity note: hook configuration lives in the same
    ``forge_config.json`` blob V1 already uses for operator config.
    """
    import json

    from qai.chat.domain.hook import HookConfig, HookEvent

    # S-7 (align D4): master execution gate. When ``chat.hooks_enabled`` is
    # False (the secure default) the engine never spawns subprocesses, even
    # if hooks are configured in forge_config.json. The Hooks tab UI stays
    # available regardless (config-only when disabled).
    #
    # Precedence (per-turn re-read UX): a live ``chat.hooks_enabled`` override
    # in forge_config.json (written by ``PUT /api/settings/chat_hooks_enabled``)
    # wins over the static ``settings.chat.hooks_enabled`` field, so flipping
    # the UI toggle takes effect on the next turn WITHOUT a restart. Absent the
    # forge_config key, the Settings field is the fallback (default False).
    chat_settings = getattr(container.settings, "chat", None)
    settings_hooks_enabled = bool(getattr(chat_settings, "hooks_enabled", False))

    try:
        cfg_path = container.data_paths.root / "config" / "forge_config.json"
        if not cfg_path.is_file():
            return build_hook_engine((), enabled=settings_hooks_enabled)
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        chat_cfg = ((raw or {}).get("chat")) or {}
        # forge_config override (if present) > settings field.
        override = chat_cfg.get("hooks_enabled")
        hooks_enabled = (
            bool(override) if isinstance(override, bool) else settings_hooks_enabled
        )
        entries = chat_cfg.get("hooks") or []
        hooks: list[HookConfig] = []
        valid_events = {e.value for e in HookEvent}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            event_val = entry.get("event")
            command = entry.get("command")
            if event_val not in valid_events or not isinstance(command, str):
                continue
            timeout_s = entry.get("timeout_s", 30.0)
            try:
                hooks.append(
                    HookConfig(
                        event=HookEvent(event_val),
                        command=command,
                        timeout_s=float(timeout_s),
                    )
                )
            except (ValueError, TypeError):
                continue
        return build_hook_engine(tuple(hooks), enabled=hooks_enabled)
    except Exception:  # noqa: BLE001 — hooks are opt-in; never break startup
        return build_hook_engine((), enabled=settings_hooks_enabled)


def _build_lazy_hook_engine(container: Container) -> Any:
    """Build a per-turn re-reading :class:`LazyReloadHookEngine`.

    Wraps :func:`_load_chat_hooks` in a zero-arg provider closure so the chat
    use case receives a hook engine that RE-READS
    ``<data>/config/forge_config.json`` (both the ``chat.hooks`` array AND the
    ``chat.hooks_enabled`` override, precedence forge_config > settings) on
    every ``has_hook`` / ``fire`` call. Saving hooks or flipping the enable
    toggle in the UI therefore takes effect on the next turn WITHOUT a service
    restart, mirroring the per-turn ``_build_runtime_debug_config_reader``
    pattern. All reads are best-effort with graceful defaults (a malformed
    config degrades to the no-op :class:`NullHookEngine`), so a turn is never
    broken. When hooks are absent / disabled the provider returns a
    :class:`NullHookEngine`, preserving the zero-cost fast path.
    """
    return LazyReloadHookEngine(lambda: _load_chat_hooks(container))


def _build_runtime_debug_config_reader(
    container: Container,
) -> Callable[[], dict[str, Any]]:
    """Build the per-turn forge-config debug-flag reader for the chat use case.

    Returns a zero-arg callable that RE-READS ``<data>/config/forge_config.json``
    on every call and returns ``{"prompt_debug": bool, "show_prompt_in_ui":
    bool}`` from the ``service_launch`` section. Re-reading per call (not
    cached) is intentional: an operator edit to forge_config.json takes effect
    on the very next message WITHOUT a service restart (V1 parity — these are
    runtime ``service_launch`` flags).

    Graceful defaults (file absent / malformed / key missing): ``prompt_debug``
    → False (no console dump), ``show_prompt_in_ui`` → True (always save the
    snapshot — the prior always-save behaviour). This callable lives at the
    ``apps/api`` composition root, so reading the forge-config document does NOT
    violate chat→user_prefs context isolation (the chat BC only sees the opaque
    callable).
    """
    import json

    def _read() -> dict[str, Any]:
        try:
            cfg_path = container.data_paths.root / "config" / "forge_config.json"
            if not cfg_path.is_file():
                return {"prompt_debug": False, "show_prompt_in_ui": True}
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            service_launch = ((raw or {}).get("service_launch")) or {}
            return {
                "prompt_debug": bool(service_launch.get("prompt_debug", False)),
                "show_prompt_in_ui": bool(
                    service_launch.get("show_prompt_in_ui", True)
                ),
            }
        except Exception:  # noqa: BLE001 — debug flags must never break a turn
            return {"prompt_debug": False, "show_prompt_in_ui": True}

    return _read


def _build_computer_enabled_reader(
    container: Container,
) -> Callable[[], bool]:
    """Build the per-turn ``computer.enabled`` gate for the chat use case.

    Returns a zero-arg SYNC callable that RE-READS
    ``<data>/config/forge_config.json`` on every call and returns the live
    desktop-control enable flag: the ``computer.enabled`` boolean written by
    ``PUT /api/settings/computer_enabled`` takes precedence; absent that key
    it falls back to the static ``settings.computer.enabled`` field. Re-reading
    per call (not cached) lets the Settings UI toggle take effect on the very
    next turn WITHOUT a restart (mirrors ``_build_runtime_debug_config_reader``
    / ``chat_hooks_enabled``).

    Graceful default (file absent / malformed / both sources unset): the
    static settings field, itself defaulting to ``False`` (secure-by-default —
    the desktop-control tool is off unless explicitly enabled). Lives at the
    ``apps/api`` composition root, so reading forge_config does NOT violate
    chat->user_prefs context isolation (chat only sees the opaque callable).
    """
    import json

    settings_default = bool(
        getattr(getattr(container.settings, "computer", None), "enabled", False)
    )

    def _read() -> bool:
        try:
            cfg_path = container.data_paths.root / "config" / "forge_config.json"
            if not cfg_path.is_file():
                return settings_default
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            section = ((raw or {}).get("computer")) or {}
            override = section.get("enabled")
            if isinstance(override, bool):
                return override
            return settings_default
        except Exception:  # noqa: BLE001 — the gate must never break a turn
            return settings_default

    return _read


def _build_cloud_image_reader(
    container: Container,
) -> Callable[[], "tuple[str, int]"]:
    """Build the per-turn cloud-image (format, quality) reader.

    Returns a zero-arg SYNC callable re-reading ``<data>/config/
    forge_config.json`` each call for ``chat.image_cloud_format`` (webp|jpeg|
    png) + ``chat.image_cloud_quality`` (1..100) — written by
    ``PUT /api/settings/image_cloud`` (Agent settings panel). Only the copy of
    a screenshot / uploaded image SENT TO THE CLOUD MODEL is re-encoded with
    these; the on-disk original (shown to the user) is untouched. Re-read per
    turn so a Settings change takes effect on the next message WITHOUT a
    restart. Graceful default (file absent / malformed / unset): ``("webp",
    50)`` — matches the encoder default. Never raises.
    """
    import json

    def _read() -> "tuple[str, int]":
        fmt, quality = "webp", 50
        try:
            cfg_path = container.data_paths.root / "config" / "forge_config.json"
            if not cfg_path.is_file():
                return fmt, quality
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            chat_cfg = ((raw or {}).get("chat")) or {}
            f = chat_cfg.get("image_cloud_format")
            if isinstance(f, str) and f.strip().lower() in (
                "webp", "jpeg", "jpg", "png"
            ):
                fmt = f.strip().lower()
            q = chat_cfg.get("image_cloud_quality")
            if isinstance(q, (int, float)):
                quality = max(1, min(100, int(q)))
        except Exception:  # noqa: BLE001 — config read must never break a turn
            pass
        return fmt, quality

    return _read


def _build_compaction_ratio_reader(
    container: Container,
) -> Callable[[], Awaitable[dict[str, float]]]:
    """Build the per-compaction context-compression ratio reader.

    Returns a zero-arg ASYNC callable returning ``{"target": float,
    "protect": float}`` from the user_prefs ``forge.config`` ``chat.*``
    document — the SAME store the Settings -> App Config -> Agent Loop sliders
    write via ``POST /api/forge-config`` (so the sliders and the reader share
    one source of truth; no second config surface):

    * ``chat.compaction_target_ratio``  -> ``target``  (post-compression keep
      size as a fraction of the model window; default 0.35).
    * ``chat.compaction_protect_ratio`` -> ``protect`` (most-recent history
      protected verbatim as a fraction of the window; default 0.35).

    Read per compaction (not cached) so a Settings change takes effect on the
    next compaction WITHOUT a restart. ``container.user_prefs`` is wired AFTER
    chat in DI order, so it is resolved lazily at call time. Living at the
    ``apps/api`` composition root, this callable reads the user_prefs document
    WITHOUT violating chat->user_prefs context isolation (the chat BC only sees
    the opaque async dict). Graceful default ``{}`` on any failure (context
    unwired / keys missing / read error) — the use case then clamps/falls back
    to the kernel defaults (0.35 / 0.35), reproducing the prior behaviour.
    """

    async def _read() -> dict[str, float]:
        try:
            up = getattr(container, "user_prefs", None)
            if up is None:
                return {}
            load_uc = getattr(up, "load_document_use_case", None)
            if load_uc is None:
                return {}
            doc = await load_uc.execute("forge.config")
            chat_section = (doc or {}).get("chat")
            if not isinstance(chat_section, dict):
                return {}
            out: dict[str, float] = {}
            target = chat_section.get("compaction_target_ratio")
            protect = chat_section.get("compaction_protect_ratio")
            if isinstance(target, (int, float)):
                out["target"] = float(target)
            if isinstance(protect, (int, float)):
                out["protect"] = float(protect)
            return out
        except Exception:  # noqa: BLE001 — pref read must never break a run
            return {}

    return _read

#: Bounds enforced on the auto-background threshold regardless of origin.
#: Mirrors ``ChatSettings.bash_auto_background_threshold_ms``' own
#: ``ge``/``le``, re-stated here because ``forge_config`` is a user-writable
#: JSON document that no DTO validates.
_AUTO_BG_THRESHOLD_BOUNDS = (1_000, 600_000)

#: Distinguishes "user explicitly switched auto-background OFF" from "nothing
#: configured" inside the reader -> wrapper handoff.  Both end up as ``None``
#: for the use case, but only the latter may consult the ``ChatSettings``
#: layer, so the two cases must stay separable across that boundary.
_DISABLED_SENTINEL = -1


def _build_auto_bg_threshold_reader(
    container: Container,
) -> Callable[[], Awaitable[int | None]]:
    """Build the per-turn auto-background threshold reader (M5 full alignment).

    Returns a zero-arg ASYNC callable resolving the effective threshold from
    ``forge.config``'s ``chat`` section, which carries TWO fields written by
    the Settings -> Agent tab UI:

    * ``bash_auto_background_enabled`` (bool) — the master switch.  ``False``
      resolves to ``None``, which makes the exec race SKIP its threshold arm
      while completion / Stop / injection keep racing normally.  The OFF
      state is a boolean, never a magic threshold value.
    * ``bash_auto_background_threshold_ms`` (int) — the wait in ms, only
      consulted when the switch is on.

    Reading per turn (not cached) mirrors the compaction-ratio pattern: a
    Settings change lands on the NEXT turn without a service restart.

    Precedence (highest wins), see ``_wrap_with_settings_fallback`` for
    layers 2/3:

    1. ``forge.config`` ``chat.bash_auto_background_*`` (user UI override)
    2. :class:`ChatSettings` ``bash_auto_background_*`` (config file / env)
    3. Neither → ``None`` reaches :class:`TurnSoftSteerCtx.threshold_ms` and
       the exec race simply runs without its threshold arm.

    This reader returns a two-state result so the wrapper can tell "user
    explicitly disabled" apart from "nothing configured":
    ``_DISABLED_SENTINEL`` for an explicit ``enabled=False``, ``None`` for
    absence / read error (wrapper then applies the ChatSettings layer).
    """

    async def _read() -> int | None:
        try:
            up = getattr(container, "user_prefs", None)
            if up is None:
                return None
            load_uc = getattr(up, "load_document_use_case", None)
            if load_uc is None:
                return None
            doc = await load_uc.execute("forge.config")
            chat_section = (doc or {}).get("chat")
            if not isinstance(chat_section, dict):
                return None
            enabled = chat_section.get("bash_auto_background_enabled")
            if enabled is False:
                _log.debug(
                    "settings.auto_background.reader_fallback layer=forge_config "
                    "resolved=disabled"
                )
                return _DISABLED_SENTINEL
            v = chat_section.get("bash_auto_background_threshold_ms")
            if enabled is True and isinstance(v, (int, float)):
                iv = int(v)
                if iv >= _AUTO_BG_THRESHOLD_BOUNDS[0]:
                    _log.debug(
                        "settings.auto_background.reader_fallback "
                        "layer=forge_config resolved=%d",
                        iv,
                    )
                    return iv
            return None
        except Exception:  # noqa: BLE001 — pref read must never break a turn
            return None

    return _read


def _wrap_with_settings_fallback(
    reader: Callable[[], Awaitable[int | None]],
    chat_settings: Any,
) -> Callable[[], Awaitable[int | None]]:
    """Wrap the forge_config reader with a ChatSettings static fallback.

    Precedence: forge_config override -> ``ChatSettings``
    ``bash_auto_background_enabled`` / ``bash_auto_background_threshold_ms``
    -> ``None``.  ``None`` is NOT a phantom default: it makes the exec race
    drop its threshold arm (no auto-background) while Stop and mid-turn
    injection keep working, so a missing setting degrades to "feature off",
    never to a threshold the Settings page never showed.

    Kept as a tiny wrapper so the reader itself stays context-pure (only
    reads forge_config); mixing the Settings fallback lives at the
    composition-root layer where both are visible.
    """
    static_fallback: int | None = None
    if getattr(chat_settings, "bash_auto_background_enabled", False) is True:
        v = getattr(chat_settings, "bash_auto_background_threshold_ms", None)
        if isinstance(v, (int, float)) and int(v) >= _AUTO_BG_THRESHOLD_BOUNDS[0]:
            static_fallback = int(v)

    async def _read() -> int | None:
        result = await reader()
        if result is _DISABLED_SENTINEL:
            _log.debug(
                "settings.auto_background.reader_fallback layer=forge_config "
                "effective=None reason=user_disabled"
            )
            return None
        if result is None:
            _log.debug(
                "settings.auto_background.reader_fallback layer=%s effective=%s",
                "ChatSettings" if static_fallback is not None else "None",
                static_fallback,
            )
            return static_fallback
        return result

    return _read


#: Bounds enforced on the recursion ceiling regardless of where the value came
#: from.  ``ChatSettings`` validates its own field, but ``forge_config`` is a
#: user-writable JSON document that no DTO guards, so a hand-edited or
#: out-of-range value is clamped here rather than trusted.
_MAX_RECURSION_DEPTH_BOUNDS = (1, 5)


def _build_max_recursion_depth_reader(
    container: Container,
    chat_settings: Any,
) -> Callable[[], Awaitable[int]]:
    """Build the per-spawn sub-agent recursion-depth reader (N.23).

    Returns a zero-arg ASYNC callable resolving the recursion ceiling the NEXT
    spawn is admitted against.  Read per spawn (not cached), the same shape
    ``_build_auto_bg_threshold_reader`` uses, so a Settings -> Agent tab change
    lands on the next turn without a service restart.

    Precedence (highest wins):

    1. ``forge.config`` ``chat.max_subagent_recursion_depth`` (user UI override)
    2. :attr:`ChatSettings.max_subagent_recursion_depth` (config file / env)

    Unlike the M5 threshold reader there is no third "give up" rung: a spawn
    MUST be admitted against some ceiling, and (2) always exists because the
    field carries a default.  The result is clamped to
    ``_MAX_RECURSION_DEPTH_BOUNDS`` — the UI writes through a plain
    ``forge_config`` document with no DTO validation, so an out-of-range value
    must not widen the ceiling past what the Settings field permits.
    """
    static_fallback = _clamp_recursion_depth(
        chat_settings.max_subagent_recursion_depth
    )

    async def _read() -> int:
        try:
            up = getattr(container, "user_prefs", None)
            if up is None:
                return static_fallback
            load_uc = getattr(up, "load_document_use_case", None)
            if load_uc is None:
                return static_fallback
            doc = await load_uc.execute("forge.config")
            chat_section = (doc or {}).get("chat")
            if not isinstance(chat_section, dict):
                return static_fallback
            v = chat_section.get("max_subagent_recursion_depth")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return _clamp_recursion_depth(v)
            return static_fallback
        except Exception:  # noqa: BLE001 — pref read must never break a spawn
            return static_fallback

    return _read


def _clamp_recursion_depth(value: Any) -> int:
    """Coerce *value* into the permitted recursion-depth band."""
    low, high = _MAX_RECURSION_DEPTH_BOUNDS
    return max(low, min(high, int(value)))



def _build_max_rounds_reader(
    container: Container,
) -> Callable[[], Awaitable[int | None]]:
    """Build the per-turn max-rounds cap reader.

    Returns a zero-arg ASYNC callable returning the user's optional
    ``agent.max_rounds`` cap from ``forge.config``, or ``None`` when the
    user has not configured a cap (= unlimited). Read per turn (not cached)
    so a Settings change takes effect on the next message WITHOUT a restart.

    The Agent Settings panel writes ``agent.max_rounds`` via
    ``POST /api/forge-config``. Values: positive int = cap; 0/absent/null =
    unlimited (no cap).
    """

    async def _read() -> int | None:
        try:
            up = getattr(container, "user_prefs", None)
            if up is None:
                return None
            load_uc = getattr(up, "load_document_use_case", None)
            if load_uc is None:
                return None
            doc = await load_uc.execute("forge.config")
            if not isinstance(doc, dict):
                return None
            agent_section = doc.get("agent")
            if isinstance(agent_section, dict):
                val = agent_section.get("max_rounds")
            else:
                val = doc.get("max_rounds")
            if isinstance(val, int) and val > 0:
                return val
            return None
        except Exception:  # noqa: BLE001 — pref read must never break a turn
            return None

    return _read


def _build_turn_warning_reader(
    container: Container,
) -> Callable[[], Awaitable[dict[str, Any]]]:
    """Build the per-turn turn-warning settings reader.

    Returns a zero-arg ASYNC callable returning the user's turn-warning
    preferences from ``forge.config`` (``turn_warning.{enabled, start,
    step}``). Read per turn so a Settings change takes effect immediately.

    Return dict keys (all optional):
      - ``enabled``: bool (default True)
      - ``start``: int — first warning threshold (user turns)
      - ``step``: int — interval between subsequent warnings
    """

    async def _read() -> dict[str, Any]:
        try:
            up = getattr(container, "user_prefs", None)
            if up is None:
                return {}
            load_uc = getattr(up, "load_document_use_case", None)
            if load_uc is None:
                return {}
            doc = await load_uc.execute("forge.config")
            if not isinstance(doc, dict):
                return {}
            tw = doc.get("turn_warning")
            if not isinstance(tw, dict):
                return {}
            out: dict[str, Any] = {}
            if "enabled" in tw and isinstance(tw["enabled"], bool):
                out["enabled"] = tw["enabled"]
            if "start" in tw and isinstance(tw["start"], int):
                out["start"] = tw["start"]
            if "step" in tw and isinstance(tw["step"], int):
                out["step"] = tw["step"]
            return out
        except Exception:  # noqa: BLE001 — pref read must never break a turn
            return {}

    return _read


def _build_subagent_profile_models_reader(
    container: Container,
) -> Callable[[], dict[str, str]]:
    """Build the per-run sub-agent per-profile model-override reader.

    Returns a zero-arg (sync) callable that RE-READS
    ``<data>/config/forge_config.json`` ``chat.subagent_profile_models`` on
    every call and returns a ``{profile_name: model_id}`` mapping (keys
    ``"general"`` / ``"explore"``). Blank / missing entries are omitted so the
    ``AgentToolHandler`` inherits the parent's ``model_hint`` for that profile
    EXACTLY as before (identity-preserving no-op). The SAME forge_config file +
    ``chat`` section the ``PUT /api/settings/subagent_profile_models`` route
    writes, so the Settings toggle and this reader share one source of truth.

    Read per call (not cached) so a Settings change takes effect on the very
    next sub-agent run WITHOUT a restart (mirrors
    ``_build_runtime_debug_config_reader``). Living at the ``apps/api``
    composition root, reading the forge-config document does NOT violate
    chat->user_prefs context isolation (the chat BC only sees the opaque
    callable). Graceful default ``{}`` on any failure (file absent / malformed
    / key missing) so a bad config never breaks a run.
    """
    import json

    def _read() -> dict[str, str]:
        try:
            cfg_path = container.data_paths.root / "config" / "forge_config.json"
            if not cfg_path.is_file():
                return {}
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            chat_cfg = ((raw or {}).get("chat")) or {}
            models = chat_cfg.get("subagent_profile_models")
            if not isinstance(models, dict):
                return {}
            out: dict[str, str] = {}
            for key in ("general", "explore"):
                value = models.get(key)
                if isinstance(value, str) and value.strip():
                    out[key] = value.strip()
            return out
        except Exception:  # noqa: BLE001 — pref read must never break a run
            return {}

    return _read


def build_chat_services(container: Container) -> ChatServices:
    """Wire ``container.chat`` with PR-042 + PR-043 + PR-401b real adapters.

    Real adapters (final state after PR-401b):

    * conversation repository (sqlite)
    * tab session store (sqlite)             -- PR-043
    * LLM stream (httpx + OpenAI-compatible SSE)
    * tool invocation (registry-backed, empty on boot)
    * stream abort registry (in-memory)
    * guardrail controller (in-memory; per-turn factory)   -- PR-401b
    * tool result truncator (adaptive; singleton)          -- PR-401b
    * retry policy (default schedule; singleton)           -- PR-401b
    * system prompt builder (static; singleton)            -- PR-401b

    No ``_Fake<Port>`` adapters remain; the regex-based lock-in test
    asserts zero ``class _Fake\\w+`` matches in this file.
    """
    clock = container.clock
    ids = container.ids
    db = container.database
    events = container.events

    # Live global Settings.ssl_verify provider (single reused pattern; mirrors
    # build_global_proxy_provider). Threaded into every outbound chat adapter
    # below (provider-routing stream, title generator, prompt enhancer, MCP
    # registry, default LLM stream) so the global SSL toggle hot-applies at
    # httpx-client-build time. The frozen ``container.settings.ssl_verify`` bool
    # is still passed for back-compat; the provider takes precedence.
    from apps.api._global_proxy import build_ssl_verify_provider

    _ssl_verify_provider = build_ssl_verify_provider(container)

    # ----- Real adapters (PR-042 + PR-043) -----
    conversations = SqliteConversationRepository(db=db)
    tabs = SqliteTabSessionStore(db=db)  # NEW (PR-043)
    # Sub-agent session persistence + participant registry (sub-agent
    # wake/resume + user takeover; groundwork for future multi-agent
    # conversations). Both follow the parent conversation's lifecycle
    # (migration 030 cascades on chat_conversation delete).
    # Per-conversation write serialiser.  Every writer that touches a
    # conversation's ``chat_message`` rows during a turn goes through this
    # one object, so same-conversation writes are ordered by construction
    # instead of racing for SQLite's write lock: the streaming turn's
    # incremental round saves + terminal save, and the background
    # dispatcher's SYSTEM_NOTICE append.  It also hands the dispatcher the
    # ack future the coordinator's ``settle_pending`` waits on.
    #
    # Built HERE, before the sub-agent session repository, because that
    # repository is wrapped in it: ``chat_subagent_session`` writes take the
    # SAME file write lock, so they must land in the SAME per-conversation
    # worker as the messages or the two contend and some exceed
    # ``busy_timeout`` (the row then simply does not exist, and the
    # sub-agent fails silently).
    #
    # No task is spawned here: this factory is synchronous, so
    # ``asyncio.create_task`` would raise ``RuntimeError: no running event
    # loop``.  The queue starts a per-conversation worker lazily on first
    # submit (every ``submit_*`` is a coroutine → a loop is guaranteed);
    # ``lifespan.on_shutdown`` calls ``close()`` to drain them.
    _subagent_sessions_store = SqliteSubAgentSessionRepository(db=db)
    conversation_write_queue = ConversationWriteQueue(
        conversations=conversations,
        sub_agent_sessions=_subagent_sessions_store,
    )
    # Everything downstream sees the SERIALISED port, so no caller — spawn
    # path, take-over turn, or the model-change REST route — can reach the
    # raw repository and re-introduce the contention.
    subagent_sessions = QueuedSubAgentSessionRepository(
        inner=_subagent_sessions_store,
        queue=conversation_write_queue,
    )
    participants = SqliteParticipantRepository(db=db)
    # CCD-5: durable backing store for StreamChatUseCase's in-memory compaction
    # checkpoint cache (migration 044). One row per conversation, cascades on
    # chat_conversation delete (foreign_keys=ON) so a deleted conversation's
    # checkpoint is removed automatically. Lets compaction state (compacted-wire
    # head + differential token baseline) survive a process restart.
    compaction_checkpoint_store = SqliteCompactionCheckpointRepository(db=db)
    # CONTEXT-COMPRESSION-NEXT §5.1: append-only JSONL journal that captures
    # one line per successful compaction (trigger inputs, before/after shape,
    # phase reached, anchors). Fire-and-forget sink — the caller's turn never
    # fails because of the journal (append swallows exceptions to debug logs).
    # Lives under ``<data_paths.root>/logs/compaction-events.jsonl`` alongside
    # other operator observability artefacts; the directory is created on
    # first construction. Attached to the streaming use case's compaction
    # engine after ``ChatServices`` is built (streaming.py is off-limits in
    # this phase — the assignment happens just before ``return`` below).
    compaction_journal = CompactionJournal(
        base_dir=container.data_paths.root / "logs",
    )
    # Roster-template library (reusable discussion "teams"; pure V2 enhancement,
    # migration 038). Conversation-INDEPENDENT (no FK) — applying a template
    # instantiates one named-agent participant per member on a target
    # conversation via the same Participant.create path the CRUD use case uses.
    roster_templates = SqliteRosterTemplateRepository(db=db)
    # Agent-template library (reusable single roles; pure V2 enhancement,
    # migration 039, §27 three-tier template system). Conversation-INDEPENDENT
    # (no FK) — applying a template instantiates one named-agent participant on
    # a target conversation via the same Participant.create path.
    agent_templates = SqliteAgentTemplateRepository(db=db)
    # Mode-template library (collaboration modes; pure V2 enhancement, migration
    # 040, §26/§27). Conversation-INDEPENDENT (no FK) — a conversation
    # references its chosen mode via meta["discussion"]["selected_mode_id"].
    mode_templates = SqliteModeTemplateRepository(db=db)
    # ----- Runtime-learned API-limit store (D2 dealign) -----
    # Process singleton: holds the mutable learned-ceiling cache that the
    # domain used to keep as a module global (and that ``llm_stream`` used
    # to keep as an infrastructure module global).  Constructed here so it
    # can be injected into the LLM stream adapter below — that adapter is
    # the sole consumer (it records ceilings from upstream 400s and clamps
    # subsequent requests), making D2's port the single source of truth.
    runtime_limit_store: RuntimeLimitStorePort = InMemoryRuntimeLimitStore()
    # Default single-endpoint transport built from chat settings; used as
    # the fallback / offline path by the provider-routing wrapper below.
    default_llm = _build_llm_stream(
        container=container,
        runtime_limit_store=runtime_limit_store,
    )
    tools = RegistryBackedToolInvocation()
    abort_registry = InMemoryStreamAbortRegistry()
    chat_stream_broadcaster = ChatStreamBroadcaster()
    # Shared with the route-layer answer endpoint so a user-submitted answer
    # can resolve the future the blocking ``question`` tool awaits.
    question_registry = InMemoryQuestionRegistry()
    # Shared with the route-layer control WS (the inject button writes here)
    # and the streaming run loop (drains it at the inter-round seam).
    injection_registry = InMemoryInjectionRegistry()
    # Exec 4-way race (Batch 5): turns the registry's inject events into the
    # per-tab ``asyncio.Event`` a running ``exec`` races on, so a Ctrl+Enter
    # injection hands a long command to the background-process manager
    # IMMEDIATELY rather than waiting out the auto-background threshold.
    # MUST observe the SAME registry instance the control WS writes to.
    injection_signal_bridge = InjectionSignalBridge(injection_registry)
    openai_model_lister = DefaultOpenAIModelListerAdapter(container=container)

    # ----- Model resolver (S9 A4 + block-2 provider routing) -----
    # Built before the LLM stream wrapper so the wrapper can route each
    # turn to the selected model's provider endpoint.
    chat_settings = getattr(container.settings, "chat", None)
    _resolver_base_url = getattr(chat_settings, "llm_base_url", None) or None
    _resolver_api_key = getattr(chat_settings, "llm_api_key", None) or None
    _resolver_model = getattr(chat_settings, "llm_default_model", None) or "qai-default"
    # Provider-aware lookup: route a selected cloud model id to its owning
    # provider's base_url + api_key.  The cross-context read of the
    # model_catalog provider registry + the platform SecretStore lives in
    # the apps bridge (qai.chat never imports model_catalog / platform).
    # Best-effort: if the model_catalog context is not wired (minimal
    # deployments / early bootstrap) the resolver degrades to the
    # settings-based default endpoint.
    _provider_lookup = None
    _model_catalog = getattr(container, "model_catalog", None)
    _provider_registry = getattr(_model_catalog, "provider_registry", None)
    if _provider_registry is not None:
        from ._model_resolver_bridge import ModelCatalogProviderLookupBridge

        _provider_lookup = ModelCatalogProviderLookupBridge(
            provider_registry=_provider_registry,
            secret_store=getattr(container, "secret_store", None),
        )
    # Local Genie service endpoint provider (V1 ``_stream_local`` parity):
    # resolves ``http://127.0.0.1:<port>/v1`` from the live model_runtime
    # daemon status / forge.config so ``local::``-prefixed models route to
    # the running local service. Cross-context read lives in the apps bridge
    # (qai.chat never imports qai.model_runtime / qai.user_prefs).
    from ._local_service_endpoint_bridge import (
        make_local_service_endpoint_provider,
    )

    _local_endpoint_provider = make_local_service_endpoint_provider(container)
    model_resolver: ModelResolverPort = ProviderAwareModelResolver(
        default_base_url=_resolver_base_url or "",
        default_api_key=_resolver_api_key or "",
        default_model=_resolver_model,
        provider_lookup=_provider_lookup,
        local_endpoint_provider=_local_endpoint_provider,
    )

    # ----- Provider-routing LLM stream (block-2 hot-path activation) -----
    # Wrap the default transport so every chat turn resolves its
    # ``model_hint`` through ``model_resolver`` and is dispatched to the
    # matching provider's base_url + api_key.  Local models keep the local
    # endpoint; unconfigured / offline falls back to ``default_llm`` which
    # already emits the offline notice.  ``StreamChatUseCase`` consumes this
    # via the unchanged ``llm`` field, so no use-case / contract change is
    # needed — the routing happens entirely inside the port adapter.
    #
    # Local on-device routing (audit Bug #1 + #2): a resolved ``is_local``
    # model is dispatched to a per-endpoint ``LocalModelStreamAdapter`` (V1
    # parity) instead of the cloud
    # HTTP transport.  The cloud transport mis-reads GenieAPIService's
    # per-frame ``finish_reason: ""`` as a normal stream end and truncates the
    # reply at the first content token; the local adapter ends only on
    # ``[DONE]``, parses inline XML ``<tool_call>`` blocks, and filters the
    # service status lines ("Processing long text..." / "Preparing
    # inference..." / "Inferencing...") out of the assistant body.  The
    # factory is wired here at the composition root because
    # ``LocalModelStreamAdapter`` lives in the ``qai.chat.adapters`` layer
    # (above ``qai.chat.infrastructure`` in the layering contract), so the
    # routing wrapper only ever sees the abstract ``LLMStreamPort``.
    # V1 ``_stream_local`` uses a long per-read timeout (default 1800s) to
    # cover long-text summarisation on-device; mirror that here so a slow NPU
    # turn is not cut off mid-stream.
    _local_stream_timeout = 1800.0

    def _local_stream_factory(resolved: Any) -> LLMStreamPort:
        return LocalModelStreamAdapter(
            base_url=resolved.base_url,
            ids=ids,
            api_key=resolved.api_key,
            model_name=resolved.model_id or "local",
            timeout_seconds=_local_stream_timeout,
        )

    # Query-service routing (internal-only): a ``query::<id>`` model hint is
    # dispatched to a QueryServiceAdapter built from edition config +
    # SecretStore by the apps-layer bridge. The bridge returns ``None`` on
    # external editions (the query_service subpackage + edition config are also
    # physically excluded), so ``query::*`` hints there fall back to the default
    # stream. Gated entirely behind ``settings.is_internal`` inside the bridge.
    from ._query_service_bridge import make_query_stream_factory

    _query_stream_factory = make_query_stream_factory(container=container, ids=ids)

    # ONE shared registry that learns each MODEL's prompt-cache read-back
    # behaviour at runtime (keyed on model_hint) and drives the tool-output
    # aging gate. Built once here and injected into the TWO collaborators that
    # consult it: the sub-agent handler and the main use case (both read
    # ``aging_enabled`` before aging + write the round's actual cache-read
    # volume back via ``mark`` on round-end). Process-lifetime, in-memory,
    # never persisted (a restart re-learns from UNKNOWN on the first round).
    provider_cache_registry = ProviderCacheCapabilityRegistry()

    llm: LLMStreamPort = ProviderRoutingLLMStream(
        default_stream=default_llm,
        model_resolver=model_resolver,
        ids=ids,
        runtime_limit_store=runtime_limit_store,
        local_stream_factory=_local_stream_factory,
        query_stream_factory=_query_stream_factory,
        # Forward the configured cloud stream base timeout so per-provider
        # transports built by ``_transport_for`` use the SAME (long, idle-gap)
        # read window as ``default_llm`` instead of falling back to the
        # adapter default.  Without this the routing wrapper's ``self._timeout``
        # is None and freshly-built provider transports silently revert to the
        # default — re-introducing the 60s truncation for any non-default
        # provider.
        timeout_seconds=_resolve_cloud_stream_timeout(
            getattr(container.settings, "chat", None)
        ),
        # Unified SSL switch: per-provider cloud transports built by the
        # routing wrapper must honour ``Settings.ssl_verify`` exactly like the
        # ``default_stream`` (see ``_build_llm_stream`` below). Omitting this
        # left every routed provider transport on httpx's ``verify=True``
        # default, so internal https gateways (cloud LLM service / internal LLM endpoint)
        # failed SSL verification on the internal edition (ssl_verify=False)
        # and looped forever on ``category=network`` connect errors.
        ssl_verify=container.settings.ssl_verify,
        # Live provider forwarded to each minted per-provider transport so the
        # global SSL toggle hot-applies to the CACHED transports at request time
        # (no cache invalidation needed).
        ssl_verify_provider=_ssl_verify_provider,
    )

    # CONTEXT-COMPRESSION-NEXT §6 (P2) + §7.1 (P3): asynchronous session-digest
    # refresh + long-turn prefix summariser.
    #
    # Constructed HERE — immediately AFTER ``llm`` (the provider-routing stream)
    # and BEFORE the sub-agent handler / streaming use case, so both share the
    # same wired instances. On every successful compaction the compaction engine
    # fires ``kick_digest_refresh`` / ``kick_turn_prefix_summary``
    # fire-and-forget; each use case summarises its slice via the shared LLM
    # stream and writes the result to its own fragment table (migrations
    # 066/067). The wire-assembly path then injects a ``[Session Digest]`` /
    # ``[Turn Prefix Summary]`` system message on subsequent turns.
    #
    # ORDERING IS LOAD-BEARING: this block MUST stay below the ``llm``
    # assignment above. It previously sat ~180 lines earlier, where ``llm`` was
    # still unbound — the resulting ``NameError`` was swallowed by the
    # ``except Exception`` below, silently disabling P2 AND P3 (both use cases
    # stayed ``None``, so ``digest_enabled`` / ``turn_prefix_enabled`` reported
    # False forever and no digest was ever produced). Do not move it back up.
    #
    # The four Markdown prompt files are shipped alongside the source (single
    # source of truth — never a schema-versioned appendix in the docs), read
    # ONCE at DI construction so a hot reload of a prompt requires a restart.
    # Missing / unreadable files degrade to ``None`` → the summarisers are
    # skipped, byte-for-byte pre-P2/P3 wire behaviour preserved.
    refresh_digest_uc: RefreshDigestUseCase | None = None
    _digest_prompts: dict[str, str] = {}
    try:
        _prompts_dir = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "qai"
            / "chat"
            / "adapters"
            / "compaction_prompts"
        )
        # Fall back to the installed package's own directory when the source
        # tree layout is not present (packaged wheel deployment). The four
        # files ride along with the ``qai.chat.adapters`` package, so we can
        # resolve them from the module's own ``__file__`` too.
        if not _prompts_dir.is_dir():
            import qai.chat.adapters as _chat_adapters_pkg  # local import
            _prompts_dir = (
                Path(_chat_adapters_pkg.__file__).parent / "compaction_prompts"
            )
        _digest_prompts = {
            PROMPT_KEY_SUMMARIZATION_SYSTEM: (
                _prompts_dir / "summarization_system.md"
            ).read_text(encoding="utf-8"),
            PROMPT_KEY_COMPACTION_SUMMARY: (
                _prompts_dir / "compaction_summary.md"
            ).read_text(encoding="utf-8"),
            PROMPT_KEY_COMPACTION_UPDATE_SUMMARY: (
                _prompts_dir / "compaction_update_summary.md"
            ).read_text(encoding="utf-8"),
            PROMPT_KEY_COMPACTION_TURN_PREFIX: (
                _prompts_dir / "compaction_turn_prefix.md"
            ).read_text(encoding="utf-8"),
        }
        refresh_digest_uc = RefreshDigestUseCase(
            llm_stream=llm,
            conversations=conversations,
            checkpoint_store=compaction_checkpoint_store,
            prompts=_digest_prompts,
        )
    except Exception as _digest_di_exc:  # noqa: BLE001 — optional feature
        # Log at WARNING (not silent): a swallowed failure here disables P2/P3
        # entirely and is invisible in the compaction logs, which is exactly
        # how the ordering bug above went unnoticed.
        _log.warning(
            "chat.di.refresh_digest_uc_unavailable",
            error=str(_digest_di_exc),
            error_type=type(_digest_di_exc).__name__,
        )
        refresh_digest_uc = None
    summarize_turn_prefix_uc: SummarizeTurnPrefixUseCase | None = None
    try:
        summarize_turn_prefix_uc = SummarizeTurnPrefixUseCase(
            llm_stream=llm,
            conversations=conversations,
            checkpoint_store=compaction_checkpoint_store,
            turn_prefix_prompt=_digest_prompts[PROMPT_KEY_COMPACTION_TURN_PREFIX],
            system_prompt=_digest_prompts[PROMPT_KEY_SUMMARIZATION_SYSTEM],
        )
    except Exception as _prefix_di_exc:  # noqa: BLE001 — optional feature
        _log.warning(
            "chat.di.summarize_turn_prefix_uc_unavailable",
            error=str(_prefix_di_exc),
            error_type=type(_prefix_di_exc).__name__,
        )
        summarize_turn_prefix_uc = None

    # ----- Agent tool (A6) -----
    # The agent tool spawns a sub-agent loop for delegated sub-tasks.
    # Registration is best-effort: if it fails, chat still works without it.
    # PR-subagent-stream: keep the handler reference so we can pass it as
    # the ``agent_event_stream`` port to ``StreamChatUseCase`` further
    # below — that gives the parent stream live SUBAGENT_* frames (V1
    # user-visible parity) instead
    # of the collapsed-string ``ToolInvocationPort.invoke`` path.
    #
    # The context compressor is built HERE (before the use case that also
    # consumes it, below) so the sub-agent loop can reuse the SAME instance:
    # V1/v0.5 ran the sub-agent with NO compression and NO per-tool output
    # cap, so a long multi-round sub-agent run with large tool outputs could
    # overflow the cloud context window. Injecting the compressor lets the
    # sub-agent compress its own wire history between rounds, exactly as the
    # parent loop does.
    context_compressor: ContextCompressionPort = ThreeLevelContextCompressor()
    # The adaptive, model-aware tool-result truncator is built HERE (ahead of
    # both the sub-agent handler below AND the ``StreamChatUseCase`` further
    # down) so the SAME singleton truncates tool results in BOTH the parent
    # agentic loop and the sub-agent loop — unified via the neutral agentic
    # kernel (``_agentic_kernel.truncate_tool_result``). Previously the
    # sub-agent used a hard-coded 2 000-char cap; now the cap is model-aware
    # (family-tiered 30k-50k, pinned to V1's 50K single-result ceiling) and
    # tunable in ONE place for both loops (user requirement).
    #
    # ``ordered_slice_cap`` is DERIVED from the operator's live ``read`` caps
    # rather than left at its default constant: it is the tail-cut ceiling for
    # ordered-slice tools (read / list / skill), and it MUST stay above the
    # largest slice ``read`` can legitimately return. A deployment that raises
    # ``read_max_bytes`` / ``read_max_lines`` would otherwise push every
    # in-spec read over a fixed ceiling, tail-cutting it and destroying read's
    # own (accurate) truncation notice. Best-effort: a hand-rolled test
    # container without ``settings.tool_output`` keeps the default.
    _ordered_slice_cap: int | None = None
    _trunc_tool_output = getattr(
        getattr(container, "settings", None), "tool_output", None
    )
    if _trunc_tool_output is not None:
        try:
            _ordered_slice_cap = ordered_slice_cap_for(
                read_max_bytes=int(_trunc_tool_output.read_max_bytes),
                read_max_lines=int(_trunc_tool_output.read_max_lines),
                read_max_line_length=int(
                    _trunc_tool_output.read_max_line_length
                ),
            )
        except (AttributeError, TypeError, ValueError):
            _ordered_slice_cap = None  # keep the dataclass default
    #
    # ``overflow_store``: the head+tail split used to discard the middle of an
    # over-budget result permanently, while the self-managing tools (exec /
    # grep) persisted their full body and left a ``read(path=...)`` hint. Handing
    # the truncator the SAME store unifies those two semantics — over-budget
    # results now carry that identical recovery footer instead of losing the
    # middle.
    #
    # DELIBERATELY LEFT ``None`` HERE. ``build_chat_services`` runs BEFORE
    # ``build_ai_coding_services`` (``di.py:_build_contexts``) and
    # ``Container.ai_coding`` is a ``field(init=False)``, so the attribute does
    # not exist yet at this point — any attempt to resolve the store now can
    # ONLY yield ``None``. The previous ``getattr(getattr(container,
    # "ai_coding", None), "tool_result_store", None)`` looked like a best-effort
    # lookup but was a guaranteed miss whose ``None`` default silently turned a
    # construction-order bug into a permanently dark feature (the overflow path
    # dropped tool-result middles for good, with no symptom).
    #
    # :func:`wire_tool_result_store_into_chat` back-fills it from ``di.py``
    # immediately after ``ai_coding`` is built — the same post-build pattern
    # :func:`wire_ai_coding_tools_into_chat` uses for the tool registry. The
    # truncator is a NON-frozen dataclass, so the hook assigns the field
    # IN PLACE on this very instance (it is shared by ``StreamChatUseCase``,
    # the sub-agent handler and the ``ChatServices`` namespace — replacing it
    # would leave those three holding a store-less copy).
    tool_result_truncator: ToolResultTruncatorPort = (
        AdaptiveToolResultTruncator(ordered_slice_cap=_ordered_slice_cap)
        if _ordered_slice_cap is not None
        else AdaptiveToolResultTruncator()
    )
    # Block 2 — process-wide sub-agent live-event broadcaster. Built HERE so
    # the sub-agent handler below can publish to it AND the ChatServices
    # namespace can expose it to the SSE route. A single instance for the
    # process lifetime (the lifespan aclose()s it at shutdown).
    subagent_stream_broadcaster = SubAgentStreamBroadcaster()
    # Block 3 — per-sub-agent cancellation registry so a standalone sub-agent
    # tab's "stop" stops ONLY that sub-agent (not the parent tab). Keyed by
    # sub-agent id; injected into the handler (registers/polls/unregisters)
    # and exposed on the namespace so the interrupt route can signal it.
    subagent_abort_registry = InMemorySubAgentAbortRegistry()
    # Async-job registry — ONE process-wide cap on concurrently running
    # asynchronous jobs (spawned sub-agents + backgrounded bash). Built HERE,
    # beside the sub-agent broadcaster/abort registry, because the same
    # sub-agent machinery is its first producer. The dead-letter evict loop is
    # NOT started here: ``build_chat_services`` is a sync function with no
    # running event loop, so ``asyncio.create_task`` would raise
    # ``RuntimeError``. The lifespan spawns it at startup instead (and stashes
    # the task on ``async_job_manager.evict_task`` for shutdown cancellation).
    async_job_manager = AsyncJobManager(
        max_running=chat_settings.async_job_max_running,
        retention_seconds=30.0,
    )
    # In-memory prompt-snapshot ring buffer (shared by the main agent AND the
    # sub-agent loop). Created HERE (before the sub-agent handler) so the
    # sub-agent can save its per-round prompt snapshots into the SAME store the
    # main agent uses — so a standalone sub-agent tab's 📄 buttons open the
    # exact same snapshot dialog (tools / sampling / accumulating wire) the
    # main agent shows, via one shared store and one shared code path.
    prompt_snapshot_store: PromptSnapshotStorePort = InMemoryPromptSnapshotStore()
    # PARALLEL-TOOL-1 — shared cross-agent tool concurrency budget. Built HERE
    # (before the sub-agent handler AND the StreamChatUseCase) so the SAME
    # instance bounds concurrent tool execution in BOTH loops: without one
    # shared budget, N sub-agents x M tools-per-round x exec subprocess would
    # fan out independently and storm the machine (design §5). Read from typed
    # ChatSettings (defaults total=8 / exec=2); 0 = unbounded. Defensive getattr
    # keeps minimal-container tests working (then unbounded).
    _tp = getattr(chat_settings, "tool_parallelism", None)

    def _budget(attr: str, default: int) -> int:
        # "Config never breaks startup": a bad/non-numeric value falls back to
        # the default rather than crashing build_chat_services.
        try:
            return int(getattr(_tp, attr, default)) if _tp is not None else default
        except (TypeError, ValueError):
            return default

    tool_concurrency = ToolConcurrencyManager(
        total=_budget("total", 8),
        exec_budget=_budget("exec_budget", 2),
    )
    # PARALLEL-TOOL-1 — shared per-path file lock so concurrent write/edit/
    # apply_patch calls to the SAME file serialise (different files still run
    # in parallel). ONE instance shared by main agent + sub-agents (wired into
    # the ai_coding tool handlers via the bridge below).
    path_lock = PathLockManager()
    # ``retry_policy`` is constructed early so BOTH the sub-agent handler (P4
    # — wraps its per-round LLM stream with the SAME
    # network_retrying_stream the main use case uses; an unwired policy
    # degrades to pass-through) and the main use case below can share the
    # SAME singleton. The constructor is stateless so the early creation
    # has no side effects.
    retry_policy: RetryPolicyPort = DefaultStreamRetryPolicy()

    # P9 — same early-construction trick for the per-turn ``GuardrailPort``
    # factory so the sub-agent (built next) can share the SAME factory the
    # main use case uses. ``InMemoryGuardrailController`` is stateless at
    # construction time (all state is per-instance after the factory is
    # called), so this declaration has no side effects.
    def _guardrail_factory() -> GuardrailPort:
        return InMemoryGuardrailController()

    # ----- Per-conversation token-budget tracker (max_budget_tokens) -----
    # Built HERE (before the sub-agent handler AND the StreamChatUseCase). Two
    # roles, one persistence-backed instance:
    #   * ``budget_tracker`` (ConversationBacked, ALWAYS) — the route-layer
    #     config + read path (``PATCH /conversations/{id}/budget`` sets the cap
    #     in ``Conversation.meta["budget"]``). Exposed on ``ChatServices`` so the
    #     route uses the PORT (never the adapter — interfaces→adapters is
    #     forbidden by import-linter).
    #   * ``enforcement_tracker`` — the SAME instance when
    #     ``Settings.chat_budget_enabled`` is True, else a no-op
    #     ``NullBudgetTracker``. Passed to the streaming use case + sub-agent
    #     handler so ENFORCEMENT is off by default (the cap can still be
    #     configured / read; it just is not applied to turns until the operator
    #     opts in). A user who never sets a cap is unaffected either way.
    budget_tracker: BudgetTrackerPort = ConversationBackedBudgetTracker(
        conversations=conversations, clock=clock
    )
    _budget_enabled = bool(
        getattr(chat_settings, "chat_budget_enabled", False)
    )
    enforcement_tracker: BudgetTrackerPort = (
        budget_tracker if _budget_enabled else NullBudgetTracker()
    )

    # ----- Unified sub-agent spawn bridge (Batch 3) -----
    # The bridge and ``AgentToolHandler`` need each other: the bridge runs a
    # sub-agent through the handler's ``iter_events``, while the handler hands
    # its own ``agent`` tool calls to the bridge.  The cycle is broken with a
    # LATE-BOUND FACTORY rather than a setter: the bridge holds a callable that
    # resolves the handler at SPAWN time, by which point ``agent_handler``
    # (assigned just below) is long since bound.  A factory keeps the bridge
    # immutable after construction — no half-initialised window where the
    # bridge exists but its handler slot is still ``None``.
    _agent_handler_holder: dict[str, "AgentToolHandler"] = {}

    def _resolve_agent_handler() -> "AgentToolHandler":
        handler = _agent_handler_holder.get("handler")
        if handler is None:
            # Only reachable if the ``AgentToolHandler`` construction below
            # failed; a spawn then fails loudly rather than silently no-oping.
            raise RuntimeError("agent tool handler is not wired")
        return handler

    # The recursion ceiling is resolved per spawn (forge_config override >
    # ChatSettings default), so a Settings -> Agent tab change lands on the
    # next spawn without a restart.  Passed to the bridge, which hands it to
    # its ``SpawnPreflight``.
    _max_recursion_depth_reader = _build_max_recursion_depth_reader(
        container, chat_settings
    )
    sub_agent_bridge = SubAgentBridge(
        async_job_manager=async_job_manager,
        agent_tool_handler_factory=_resolve_agent_handler,
        subagent_abort_registry=subagent_abort_registry,
        ids=ids,
        chat_settings=chat_settings,
        max_depth_reader=_max_recursion_depth_reader,
    )

    agent_handler: AgentToolHandler | None = None
    try:
        agent_handler = AgentToolHandler(
            llm=llm,
            tool_executor=tools,
            prompt_snapshot_store=prompt_snapshot_store,
            # Align the sub-agent round ceiling with the MAIN agent's
            # unlimited default. The sub-agent default
            # (``_DEFAULT_SUB_AGENT_MAX_ROUNDS=15``) was far too low for the
            # heavy multi-round investigation / refactor tasks the main agent
            # delegates — a sub-agent doing real work hit the 15-round ceiling
            # and returned ``[sub-agent INCOMPLETE ...]`` (the reported
            # "15轮异常中断"). ``0`` = unlimited (kernel convention), same as
            # the main agent — the no-progress circuit breaker prevents true
            # runaway loops. Note: EXPLORE profile still has its own
            # ``max_rounds=5`` (profile-level override takes precedence).
            max_rounds=0,
            compressor=context_compressor,
            tool_result_truncator=tool_result_truncator,
            # Differential-checkpoint compaction (V2 enhancement): the sub-agent
            # NEWs its own ``CompactionCheckpointEngine`` internally (ephemeral,
            # in-memory) sharing the SAME ``compressor`` instance the main use
            # case shares (passed above) + the SAME user-chosen Agent-Loop ratios
            # via this reader — so the sub-agent and the main agent compact with
            # BYTE-FOR-BYTE the same algorithm + sliders. The reader is the SAME
            # ``_build_compaction_ratio_reader`` the main use case uses
            # (single source of truth — the forge.config ``chat.*`` doc).
            compaction_ratio_provider=_build_compaction_ratio_reader(container),
            sub_agent_sessions=subagent_sessions,
            stream_broadcaster=subagent_stream_broadcaster,
            abort_registry=subagent_abort_registry,
            tool_concurrency=tool_concurrency,
            # P4 — share the SAME retry policy + abort-aware sleeper the main
            # use case uses, so the sub-agent's per-round LLM stream gets the
            # SAME indefinite NETWORK auto-retry + abortable backoff. Default
            # ``frame_stall_budget_s`` (600s) matches the main agent's value.
            retry_policy=retry_policy,
            # P9 — share the SAME per-turn ``GuardrailPort`` factory the
            # main use case uses. Each sub-agent run instantiates its own
            # controller (per-turn accumulators do not leak across runs).
            guardrail_factory=_guardrail_factory,
            # Alpha unified-spawn-path (migration 049): the static recursion
            # ceiling from ``ChatSettings.max_subagent_recursion_depth``
            # (default 2). This is the handler's OWN in-loop cap for a nested
            # ``agent`` tool call; the live per-spawn ceiling the bridge admits
            # against comes from ``_max_recursion_depth_reader`` above (which
            # also honours the user's UI override). This replaces the pre-α
            # hard ``allow_spawn=False`` guard that used to cap recursion at
            # exactly 2 layers with no per-level knob.
            max_subagent_recursion_depth=(
                chat_settings.max_subagent_recursion_depth
            ),
            # SAME shared per-model cache-capability registry the main use case
            # marks into, so the sub-agent gates its own aging on the model's
            # learned cache-read behaviour and writes its round-end signal back
            # into the SAME process-shared learning.
            provider_cache_registry=provider_cache_registry,
            # Per-profile model override (V2 enhancement — per-profile model).
            # A per-run reader of forge_config.json ``chat.subagent_profile_models``
            # ({profile_name: model_id}, keys "general"/"explore"; blank/missing
            # = inherit the parent's model_hint). Read per run (not cached) so a
            # Settings change (PUT /api/settings/subagent_profile_models) takes
            # effect on the next sub-agent run WITHOUT a restart; best-effort ({}
            # on any error) so a bad config never breaks a run.
            profile_model_overrides_provider=(
                _build_subagent_profile_models_reader(container)
            ),
            # Per-conversation token-budget ENFORCEMENT: gated instance (Null
            # when ``chat_budget_enabled`` is off) shared with the main use case
            # so a sub-agent draws from the PARENT conversation's pool.
            budget_tracker=enforcement_tracker,
            # P2 tech-debt fix (sub-agent journal wiring): the SAME journal
            # instance the main use case receives, so sub-agent compaction
            # emits one-line-per-event JSONL alongside the main agent's
            # events (CONTEXT-COMPRESSION-NEXT §5.1). Fire-and-forget; None
            # disables emission and restores byte-for-byte prior behaviour.
            journal=compaction_journal,
            # BackgroundJobDispatcher wiring (§6.4): the sub-agent kernel
            # publishes ``SubAgentSessionTerminated`` here on every terminal
            # transition so the process-scoped dispatcher can append a
            # SYSTEM_NOTICE to the parent conversation and notify the
            # coordinator to wake the main agent if it has already unwound.
            event_bus=events,
            # Unified spawn path: a sub-agent's OWN ``agent`` tool call goes
            # through the bridge, so it too spawns in the background instead of
            # blocking its parent.  ``parent_depth=0`` — this is the MAIN
            # agent's handler, so the sub-agents it spawns sit at depth 1.
            bridge=sub_agent_bridge,
            parent_depth=0,
        )
        # Close the late-bound cycle: the bridge's factory resolves this
        # handler at spawn time.
        _agent_handler_holder["handler"] = agent_handler
        tools.register("agent", agent_handler.execute)
    except Exception:  # noqa: BLE001 — optional feature; never blocks startup
        agent_handler = None

    # ----- Harness control tools (V2 enhancement; V1 has no equivalent) -----
    # ``todowrite`` (live task-list panel) and ``question`` (blocking dialog)
    # are registered directly on the chat-side registry — like ``agent`` and
    # unlike the ai_coding file tools — because they drive UI surfaces rather
    # than performing filesystem/exec work.  Both carry their OpenAI schema so
    # they are advertised on ``payload["tools"]``.  The ``question`` handler
    # shares ``question_registry`` with the answer endpoint and observes
    # ``abort_registry`` so a "stop" cancels a pending question.
    tools.register(
        "todowrite",
        TodoWriteToolHandler().execute,
        schema=TODOWRITE_TOOL_SCHEMA,
    )
    # ``question`` hard-cap (seconds) is operator-tunable via
    # ``ChatSettings.question_timeout_seconds`` (default ``0`` = no cap, so the
    # dialog waits until answered/aborted and is never auto-cancelled; set a
    # positive value to make forgotten dialogs auto-expire). Defensive getattr
    # so deployments without chat settings keep the handler's own default.
    # ``None`` lets the handler fall back to its built-in default.
    _question_timeout = getattr(chat_settings, "question_timeout_seconds", None)
    tools.register(
        "question",
        QuestionToolHandler(
            registry=question_registry,
            abort_registry=abort_registry,
            timeout_seconds=_question_timeout,
        ).execute,
        schema=QUESTION_TOOL_SCHEMA,
    )
    # ``sub_agent`` (unified management tool): observation + control
    # surface for background sub-agents.  Actions: ``list`` / ``status``
    # / ``inspect`` (full transcript) / ``wait`` / ``wait_any`` / ``stop``.
    # Steering / resume is the ``agent`` tool's ``resume_subagent_id``
    # param — one channel for both "wake up parked sub-agent" and "add
    # more instructions" (mirrors reference design's ``hub send``
    # semantics).  Older ``list_subagents`` / ``subagent_status`` tools
    # are removed (this tool covers both plus more).
    from qai.chat.adapters.sub_agent_tool_handler import (
        SUB_AGENT_TOOL_SCHEMA,
        SubAgentToolHandler,
    )

    _sub_agent_handler = SubAgentToolHandler(
        sub_agent_sessions=subagent_sessions,
        stream_broadcaster=subagent_stream_broadcaster,
        abort_registry=subagent_abort_registry,
        # Job-aware observation + control: ``list`` / ``status`` annotate the
        # admission state (queued / running) and ``wait`` observes completion
        # instead of polling.  ``stop`` goes through the bridge because only it
        # both transitions the job (releasing the admission slot so a queued
        # sub-agent can start) AND signals the per-sub-agent abort flag.
        async_job_manager=async_job_manager,
        bridge=sub_agent_bridge,
        # ``dispatcher`` is wired below AFTER
        # ``background_job_dispatcher`` is constructed — we cannot
        # inject it here because the dispatcher itself takes the
        # ``AgentSessionCoordinator``, which is late-bound to
        # ``stream_chat_use_case``.  Setting the attribute directly
        # is safe (the class exposes ``_dispatcher`` via ``__slots__``
        # and reads it defensively via ``getattr``).
    )
    tools.register(
        "sub_agent",
        _sub_agent_handler.execute,
        schema=SUB_AGENT_TOOL_SCHEMA,
    )

    # ``skill`` (V2 enhancement): a read-only tool letting the model pull in
    # one named skill's SKILL.md instruction set on demand (instead of carrying
    # every skill's full prose in the system prompt). The handler resolves the
    # requested name against an injected ``{skill_id: skill_md_path}`` mapping
    # and returns the file text wrapped in a ``<skill_content>`` envelope; it
    # NEVER executes any code from the skill. The mapping is sourced from the
    # SAME ``SkillDiscovery`` the prompt catalog provider uses (reused via the
    # lazy ``_user_prefs_skill_discovery_factory`` defined below). Lazy because
    # ``container.user_prefs`` is wired AFTER chat in di.py; the lookup callable
    # resolves the discovery per tool call and degrades to "no skills available"
    # if user_prefs is not (yet) wired. The handler reads SKILL.md as UTF-8.
    from ._chat_skill_tool import build_skill_tool_lookup

    def _skill_discovery_for_tool() -> Any:
        up = getattr(container, "user_prefs", None)
        if up is None:
            raise RuntimeError("user_prefs not wired")
        return up.skill_discovery

    tools.register(
        "skill",
        SkillToolHandler(
            skill_lookup=build_skill_tool_lookup(_skill_discovery_for_tool),
        ).execute,
        schema=SKILL_TOOL_SCHEMA,
    )

    # ``implementation_plan`` (DISC-1 coordinator-plan-rework; discussion-only):
    # a one-shot tool the discussion coordinator (主持人) calls to register a
    # LIGHTWEIGHT structured feature-item checklist into
    # ``meta["discussion"]["implementation"]`` (reusing the existing
    # FeatureItem / write_implementation_plan data model — no text parsing).
    # It is advertised ONLY inside a discussion turn: the tool name is in
    # ``CONDITIONAL_TOOL_NAMES`` so the main-agent / sub-agent
    # ``compose_advertised_tools`` path drops it; the discussion orchestrator's
    # ``_speaker_tool_schemas`` re-surfaces it for the coordinator (whose
    # default ``allowed_tools`` names it). Shares the SAME conversation repo /
    # clock / id generator the discussion orchestrator uses so a plan
    # registered here is byte-identical to one persisted by ``_persist_plan``.
    tools.register(
        "implementation_plan",
        ImplementationPlanToolHandler(
            conversations=conversations,
            clock=clock,
            ids=ids,
        ).execute,
        schema=IMPLEMENTATION_PLAN_TOOL_SCHEMA,
    )

    # ``search_conversations`` (experience-recall replacement): an
    # agent-driven full-text search over the ``chat_message`` table so the
    # model can recall how a similar problem was handled in a PAST
    # conversation. Replaces the removed auto-extract/auto-recall experience
    # system with an on-demand tool (the model decides when to look back,
    # instead of an always-injected ``<past_experiences>`` block). Shares the
    # SAME ``db`` the conversation repository uses; reads the ``chat_message_fts``
    # FTS5 index (migration 016) with BM25 ranking, CJK bigram preprocessing,
    # and a LIKE fallback. Default scope EXCLUDES the current conversation
    # (already in the model's context window).
    tools.register(
        "search_conversations",
        SearchConversationsToolHandler(
            repository=SqliteConversationSearchRepository(db=db),
        ).execute,
        schema=SEARCH_CONVERSATIONS_TOOL_SCHEMA,
    )

    # ``remember_experience`` / ``recall_experience`` (agent-editable
    # long-term lessons): an on-demand write+recall pair over the
    # ``chat_experience`` table (migration 002) + its FTS5 index
    # (``experience_fts``, migration 009) with recall-ranking metadata
    # (migration 064). Deliberately NOT auto-injected into the system prompt
    # (mirrors search_conversations): the model saves a distilled lesson when
    # it judges one worth keeping and recalls the relevant handful by intent,
    # so a large corpus stays useful instead of a blind always-injected block
    # that misses the current need and burns context. Shares the SAME ``db``
    # the conversation repository uses.
    tools.register(
        "remember_experience",
        RememberExperienceToolHandler(
            repository=SqliteExperienceRepository(db=db),
        ).execute,
        schema=REMEMBER_EXPERIENCE_TOOL_SCHEMA,
    )
    tools.register(
        "recall_experience",
        RecallExperienceToolHandler(
            recall=SqliteExperienceRecall(db=db),
        ).execute,
        schema=RECALL_EXPERIENCE_TOOL_SCHEMA,
    )

    # ``scheduled_task`` (scheduled-task feature): lets the model schedule a
    # self-contained prompt to run later (interval / one-shot / cron). The
    # runtime-dynamic scheduler ticks in the background and, for each due task,
    # drives ONE native chat-agent turn (tools + history + multi-round loop) on
    # the task's bound conversation via ``ScheduledTaskRunner`` →
    # ``StreamChatUseCase.collect_completion``, routed with the model the task
    # captured at create time (see the handler's ``conversations``
    # model-capture). The turn persists its own user prompt + assistant reply
    # and emits the live ``ChatStream*`` events internally, so the result lands
    # in the conversation as a genuine agent turn. The scheduler also publishes
    # ``ScheduledTaskFired`` on the events bus (notification-center seam,
    # observed by ``ScheduledTaskAsideBridge``). The scheduler + bridge are
    # STARTED by the lifespan (they need a running loop); here we only construct
    # + register the tool. Gated on ``llm`` — no LLM port means nothing to run,
    # so the feature stays dark.
    #
    # Ordering: the runner depends on ``stream_chat_use_case``, which is
    # constructed further below (at the ``ChatServices(...)`` assembly). The
    # scheduler's ``executor`` is only ever invoked at tick time (background,
    # well after startup wiring completes), so we bind it here to a late
    # closure over ``_sched_runner_holder`` and populate that holder right
    # after ``stream_chat_use_case`` exists — no forward-reference NameError.
    scheduling_scheduler: SchedulerService | None = None
    scheduling_aside_bridge: ScheduledTaskAsideBridge | None = None
    _sched_runner_holder: dict[str, ScheduledTaskRunner] = {}

    async def _sched_executor(task: ScheduledTask) -> tuple[bool, str]:
        runner = _sched_runner_holder.get("runner")
        if runner is None:  # pragma: no cover — startup wiring guarantees it
            return False, "scheduled task runner not wired"
        return await runner.run(task)

    _sched_store_for_services: "SqliteScheduledTaskStore | None" = None
    _sched_tool_handler: "ScheduledTaskToolHandler | None" = None
    try:
        _sched_store = SqliteScheduledTaskStore(db=db)
        scheduling_scheduler = SchedulerService(
            store=_sched_store,
            bus=events,
            executor=_sched_executor,
            clock=clock,
        )
        # Fan the fire out to the channel user bound to the task's
        # conversation (Feishu / WeChat): a scheduled task created FROM a
        # channel fires headlessly (the runner bypasses the SSE/WS routes that
        # normally trigger the WebUI channel-sync push), so without this the
        # channel user would never be notified in-app. Best-effort + isolated
        # to the apps layer (the bridge itself never imports ``qai.channels``).
        async def _sched_push_to_channels(ev: ScheduledTaskFired) -> None:
            await push_scheduled_task_to_channels(container=container, event=ev)

        scheduling_aside_bridge = ScheduledTaskAsideBridge(
            event_bus=events,
            push_cb=_sched_push_to_channels,
        )
        # Lazy provider for the user's globally-selected model
        # (``ui.preferences.selected_model_id`` — the SAME id the WebUI sends
        # as ``model_hint`` on a live turn). Read at CREATE time (long after
        # startup), so ``container.user_prefs`` — built AFTER ``chat`` in the
        # DI order — is present by then; a lazy deref avoids the build-order
        # forward reference. Lets a task created on the FIRST turn of a new
        # conversation still capture a working model instead of falling back to
        # the offline default. Best-effort: any failure degrades to None.
        async def _selected_model_provider() -> str | None:
            prefs_ns = getattr(container, "user_prefs", None)
            if prefs_ns is None:
                return None
            try:
                doc = await prefs_ns.load_document_use_case.execute("ui.preferences")
            except Exception:  # noqa: BLE001 — best-effort
                return None
            if not isinstance(doc, dict):
                return None
            mid = doc.get("selected_model_id")
            return mid if isinstance(mid, str) and mid else None

        _sched_tool_handler = ScheduledTaskToolHandler(
            scheduler=scheduling_scheduler,
            store=_sched_store,
            ids=ids,
            clock=clock,
            conversations=conversations,
            default_model_provider=_selected_model_provider,
        )
        tools.register(
            "scheduled_task",
            _sched_tool_handler.execute,
            schema=SCHEDULED_TASK_TOOL_SCHEMA,
        )
        _sched_store_for_services = _sched_store
    except Exception:  # noqa: BLE001 — optional feature; never blocks startup
        scheduling_scheduler = None
        scheduling_aside_bridge = None
        _sched_store_for_services = None
        _sched_tool_handler = None

    # ----- ai_coding 9 production tools bridge (Batch B / B-1) -----
    # The chat ``ToolInvocationPort`` is empty by default; legacy v1 chat
    # could call read / write / edit / glob / grep / exec / web_fetch /
    # apply_patch / appbuilder_run because the same handler set lived in
    # ``backend/tools/``.  In v2 those handlers live in ``qai.ai_coding``;
    # ``qai.chat`` cannot import them directly (``context-isolation``
    # contract).  ``apps.api._chat_tool_bridge`` is the apps-layer bridge
    # that registers each ai_coding handler under its tool name on the
    # chat-side registry, so an LLM-emitted ``tool_call`` frame for
    # ``read``/``glob``/... resolves to the real implementation instead
    # of falling through to ``chat.tool_not_registered``.
    #
    # Lazy resolution: ``container.ai_coding`` may not be wired at the
    # moment ``build_chat_services`` runs (DI order in ``apps/api/di.py``
    # builds ``chat`` BEFORE ``ai_coding``).  In production the registration
    # is therefore a no-op here; the real registration happens via the
    # post-build hook ``wire_ai_coding_tools_into_chat`` called from
    # ``di.py`` immediately after ``build_ai_coding_services`` completes.
    # This early-registration path is kept as a fallback for test setups
    # that wire ``ai_coding`` before calling ``build_chat_services``
    # (e.g. integration tests that construct the container manually).
    # ``register`` is idempotent (overwrite), so double-registration is safe.
    _ai_coding_for_tools = getattr(container, "ai_coding", None)
    if _ai_coding_for_tools is not None:
        from ._chat_tool_bridge import register_ai_coding_tools_into_chat

        # NOTE: in production this branch does NOT run — ``build_chat_services``
        # executes BEFORE ``build_ai_coding_services`` (di.py), so
        # ``container.ai_coding`` is None here. The AUTHORITATIVE tool
        # registration (with the SAME ``path_lock``) happens in the post-build
        # hook ``wire_ai_coding_tools_into_chat`` below. This branch is a
        # fallback for tests that hand-build a container with ai_coding ready.
        register_ai_coding_tools_into_chat(
            tools=tools,
            file_guard=_ai_coding_for_tools.file_guard,
            file_broker=_ai_coding_for_tools.file_broker,
            tool_result_store=getattr(
                _ai_coding_for_tools, "tool_result_store", None
            ),
            path_lock=path_lock,
        )

    # ----- Real adapters (PR-401b) -----
    # The guardrail controller is per-turn (the legacy
    # ``GuardrailController()`` is instantiated inside ``stream_chat``),
    # exposed here as a zero-arg factory so ``StreamChatUseCase`` can
    # mint one whenever it begins a new agentic loop without holding
    # cross-turn state.  The other three adapters carry no mutable state
    # and are wired as singletons.
    # P9 — ``_guardrail_factory`` was constructed early (before
    # ``agent_handler``) so the sub-agent handler can share the SAME
    # factory the main use case uses; this is the existing symbol from
    # that earlier definition, NOT a re-declaration.

    # ``tool_result_truncator`` was already built above (so the sub-agent
    # handler can reuse the SAME singleton — unified per the neutral kernel).
    # ``retry_policy`` was likewise built early (P4) so the sub-agent handler
    # could share it; no second construction here — the existing
    # ``retry_policy`` symbol is the singleton.
    # Operator hooks (chat action hooks): read the
    # optional ``chat.hooks`` array from forge_config.json.  No-op engine
    # when none configured (zero cost). Wrapped in a LazyReloadHookEngine so
    # the injected engine RE-READS forge_config.json (both ``chat.hooks`` AND
    # the ``chat.hooks_enabled`` override, precedence forge_config > settings)
    # on every use — saving hooks / flipping the enable toggle in the UI takes
    # effect on the next turn WITHOUT a service restart (mirrors the per-turn
    # ``_build_runtime_debug_config_reader`` pattern; all reads best-effort so
    # a turn is never broken).
    hook_engine = _build_lazy_hook_engine(container)

    # ----- Code-persona resolver bridge (R12 dealign) -----
    # Cross-BC adapter resolving a selected code persona id against the
    # user_prefs ``ui.code_personas`` document.  Lazily reaches
    # ``container.user_prefs`` (wired after chat in DI order) at call
    # time, so the chat use case can inject the persona without importing
    # ``qai.user_prefs`` (replaces the SSE / WS route-layer resolution).
    from ._code_persona_bridge import CodePersonaResolverBridge

    _code_persona_resolver = CodePersonaResolverBridge(container=container)

    # ----- System prompt builder (Batch B / B-1 + B-2 + B-3) -----
    # The builder injects two pieces of context into every default /
    # app-builder / feature turn:
    #
    # 1. ``skill_catalog_provider`` — zero-arg callable returning live
    #    ``((path, use_for), ...)`` rows from
    #    ``container.user_prefs.skill_discovery`` filtered by per-skill
    #    ``forge.config skills.overrides`` mode.  Empty before the
    #    user_prefs context is wired (DI order: chat is built first).
    # 2. ``app_builder_skill_catalog`` — cross-context port that
    #    resolves App Builder Pack metadata for ``tool_mode == "app-builder"``.
    #    Wired via :class:`AppBuilderSkillCatalogAdapter` whose two
    #    callables read from ``container.app_builder.build_system_prompt_use_case``.
    #
    # Both are best-effort: if a context is not yet built or a
    # call raises, the relevant block is silently omitted from the
    # prompt and the rest of the assembly proceeds (existing tests that
    # construct minimal containers continue to pass).
    #
    # Tools are NOT injected into the prompt body — see the note below
    # (cloud advertises tools via ``payload["tools"]`` only).
    from ._chat_feature_skill_provider import TOOL_MODE_DIR_MAP, FeatureSkillProvider
    from ._chat_skill_catalog_provider import (
        ChatSkillCatalogProvider,
        LocalChatSkillCatalogProvider,
    )
    from ._skill_registry_bridge import AppBuilderSkillCatalogAdapter

    # NOTE (2026-06-15, AGENTS.md 🟡🟡 core rule — never carry a V1 defect into
    # V2): the cloud system prompt NO LONGER embeds a ``<tools>`` XML section.
    # For an OpenAI-compatible cloud API the tools must be advertised ONLY via
    # the top-level ``payload["tools"]`` parameter (native function-calling) —
    # the model service injects them itself.  V1 additionally pasted a
    # ``<tools>`` XML copy into the prompt body
    # which is protocol misuse + harmful redundancy (wastes tokens, drifts from
    # the real ``payload["tools"]`` set — e.g. the body XML always listed the
    # conditional ``appbuilder_run`` even in plain mode where the payload omits
    # it).  Removed here so the cloud path relies solely on ``payload["tools"]``.
    # Local on-device models are unaffected: their prompt body never carried the
    # ``<tools>`` XML either; their tools go through ``payload["tools"]`` and the
    # daemon's PromptOptimizer formats them for the on-device model.

    # skill_catalog_provider — lazy factories so user_prefs being
    # wired *after* chat (apps/api/di.py:174 vs :179) is OK.  The
    # provider is called per-request so it always sees the freshest
    # ``forge.config skills.overrides`` snapshot.
    def _user_prefs_skill_discovery_factory() -> Any:
        up = getattr(container, "user_prefs", None)
        if up is None:
            raise RuntimeError("user_prefs not wired")
        return up.skill_discovery

    def _user_prefs_load_doc_factory() -> Any:
        up = getattr(container, "user_prefs", None)
        if up is None:
            raise RuntimeError("user_prefs not wired")
        return up.load_document_use_case

    _skill_catalog_provider = ChatSkillCatalogProvider(
        skill_discovery_factory=_user_prefs_skill_discovery_factory,
        load_prefs_factory=_user_prefs_load_doc_factory,
    )

    # 2b. local_skill_catalog_provider — same discovery + override merge,
    # but filtered to LOCAL-visible skills (mode in {local, both}) and
    # returning ``(skill_id, use_for, path)`` triples so the simplified
    # local system prompt (built in the chat use case) can render the
    # V1 ``<available_skills>`` XML (<name>/<description>/<location>).
    # V1 parity: ``_build_local_messages``
    # + ``build_available_skills_xml`` (model_type
    # ="local").  The on-device model reads each <location> SKILL.md on
    # demand rather than receiving the full cloud catalog/few-shot prose.
    _local_skill_catalog_provider = LocalChatSkillCatalogProvider(
        skill_discovery_factory=_user_prefs_skill_discovery_factory,
        load_prefs_factory=_user_prefs_load_doc_factory,
    )

    # 3. App-Builder Pack catalog adapter — lazy callables so app_builder
    # being wired *after* chat (apps/api/di.py:174 vs :175) is OK.  Both
    # callables tolerate ``app_builder`` being absent (return empty).
    def _app_builder_skill_files_resolver(
        tool_mode: str,
        tool_params: dict[str, Any] | None,
    ) -> tuple[str, ...]:
        # V1 ``app_builder.skill_resolver.resolve_skill_files`` parity:
        # for the app-builder tool mode, inline the top-level App Builder
        # SKILL plus the *currently selected* Pack's SKILL (gated by the
        # Pack manifest's ``skill.enabled``). ``tool_params`` carries the
        # selected model under ``selected_model_id``. We delegate to the
        # app_builder context's :class:`ResolveSkillFilesUseCase` (wired in
        # ``apps/api/_app_builder_di.py``) so chat never imports
        # ``qai.app_builder`` directly. Returns absolute, existing path
        # strings the prompt builder ``open()``s; empty when the context /
        # use case is unavailable or the mode is not app-builder.
        if tool_mode != "app-builder":
            return ()
        ab = getattr(container, "app_builder", None)
        if ab is None:
            return ()
        uc = getattr(ab, "resolve_skill_files_use_case", None)
        if uc is None:
            return ()
        try:
            return tuple(uc.execute(tool_params))
        except Exception:  # noqa: BLE001 — best-effort cross-context
            return ()

    async def _app_builder_pack_catalog_provider() -> str:
        ab = getattr(container, "app_builder", None)
        if ab is None:
            return ""
        # V1 ``generate_pack_catalog_prompt`` parity: the capability list
        # of every enabled Pack (I/O / params / metrics / ratings / variants
        # + usage rules). Prefer the dedicated catalog use case; it replaces
        # the prior SKILL-body aggregation so the LLM sees the *capability
        # manifest* (not duplicated SKILL prose — those are injected
        # per-selected-model by the resolver above).
        uc = getattr(ab, "generate_pack_catalog_use_case", None)
        if uc is None:
            return ""
        try:
            return await uc.execute() or ""
        except Exception:  # noqa: BLE001 — best-effort cross-context
            return ""

    async def _app_builder_model_code_provider(
        tool_params: dict[str, Any] | None,
    ):
        # Multi-model chat-prompt support: for the currently selected
        # model(s) (``tool_params["selected_model_id"]`` and/or
        # ``["selected_model_ids"]``), return each model's reference
        # inference code (``runner.py``, capped) so the Agent can help the
        # user build a WebUI around them. Delegates to the app_builder
        # context's :class:`ResolveModelInferenceCodeUseCase` so chat never
        # imports ``qai.app_builder`` directly. Returns app_builder
        # ``ModelInferenceCode`` items which the adapter maps to the
        # chat-side ``AppBuilderModelCode`` DTO. Empty when the context /
        # use case is unavailable.
        ab = getattr(container, "app_builder", None)
        if ab is None:
            return ()
        uc = getattr(ab, "resolve_model_inference_code_use_case", None)
        if uc is None:
            return ()
        try:
            return await uc.execute(tool_params)
        except Exception:  # noqa: BLE001 — best-effort cross-context
            return ()

    _app_builder_catalog_adapter = AppBuilderSkillCatalogAdapter(
        resolver=_app_builder_skill_files_resolver,
        catalog_provider=_app_builder_pack_catalog_provider,
        code_provider=_app_builder_model_code_provider,
    )

    # 4. feature_skill_provider — Batch D / D-1.  Reads
    # ``<repo_root>/factory/chat_features/<dir>/SKILL.md`` fresh on each
    # call so the ~70 KB model-builder operations guide is injected when the
    # user activates the model-build toolbar mode.  Mirrors V1
    # ``get_feature_prompt`` no-cache
    # semantics (edit SKILL.md → next request sees the change).
    # The legacy ``features/`` directory was removed in the S8 cutover; the
    # three chat tool-mode skill packs (model-builder / ppt-gen / code-assist)
    # now live under ``factory/chat_features/`` so they are shipped by the
    # release manifest's [include] and survive the zero-legacy deletion.
    _features_dir = container.repo_root / "factory" / "chat_features"
    # Resolve the configured model-builder workspace root (forge.config
    # override → platform Settings default) so the SKILL.md ``${WORKSPACE}``
    # placeholder is substituted with the real path at injection time.
    # Best-effort: never break chat wiring on a config read failure.
    try:
        from ._workspace_resolver import resolve_workspace_root

        _workspace_root = resolve_workspace_root(container)
    except Exception:  # noqa: BLE001
        _workspace_root = None
    _feature_skill_provider = FeatureSkillProvider(
        features_dir=_features_dir,
        workspace_root=_workspace_root,
        app_root=str(container.repo_root),
        tool_mode_dir_map=TOOL_MODE_DIR_MAP,
    )

    system_prompt_builder: SystemPromptBuilderPort = RichSystemPromptBuilder(
        skill_catalog_provider=_skill_catalog_provider,
        app_builder_skill_catalog=_app_builder_catalog_adapter,
        feature_skill_provider=_feature_skill_provider,
        model_build_workspace_root=_workspace_root,
        app_root=str(container.repo_root),
    )


    # ----- Real adapters (PR-402) -----
    # Title generator: prefer the provider-aware HTTP adapter so first-round
    # auto-title summarisation works against a configured cloud model (e.g.
    # cloud LLM service) even when the static ``llm_base_url`` is empty — mirroring
    # the prompt-enhancer wiring below and V1 (resolved
    # the upstream from the cloud model registry, not a single hard-wired
    # endpoint).  Falls back to the offline silent adapter only when NEITHER a
    # default url NOR a provider-aware resolver is available (then the use case
    # drops straight to ``fallback_title``).
    title_base_url = getattr(chat_settings, "llm_base_url", None) or None
    title_api_key = getattr(chat_settings, "llm_api_key", None) or None
    title_model = (
        getattr(chat_settings, "llm_default_model", None) or "qai-default"
    )
    title_generator: TitleGeneratorPort
    if title_base_url or _provider_lookup is not None:
        title_generator = HttpLLMTitleGenerator(
            base_url=title_base_url,
            api_key=title_api_key,
            model_name=title_model,
            model_resolver=model_resolver,
            ssl_verify=container.settings.ssl_verify,
            ssl_verify_provider=_ssl_verify_provider,  # live global SSL toggle
        )
    else:
        title_generator = OfflineTitleGenerator()

    # ----- Real adapters (PR-403) -----
    # Image upload — chat-local FS adapter writing chat-context blobs.
    # The chat context owns its upload surface; storage is resolved through
    # the ``DataPaths`` port (``blob_dir("chat")`` -> ``data/blobs/chat``)
    # rather than ad-hoc joining ``data/images`` (ARCH-2, 2026-06-09): chat
    # images are a chat-context blob and now live alongside other blobs.
    # The static-files mount (``_spa_mount``) serves this dir at the V1
    # ``/api/images/files/`` prefix (URL contract unchanged).
    images_root = container.data_paths.blob_dir("chat")
    image_upload_store: ImageUploadStorePort = FileSystemImageUploadStore(
        root=images_root,
    )
    # Prompt enhancer (V1 parity): runs against the SELECTED model's
    # upstream via the same provider-aware ``model_resolver`` the chat hot
    # path uses, so enhance works with a cloud model (e.g. cloud LLM service)
    # even when the local on-device service is offline. The static
    # ``title_base_url`` only serves as the fallback default endpoint. Use
    # the HTTP enhancer whenever EITHER a default url OR a provider-aware
    # resolver is available.
    prompt_enhancer: PromptEnhancerPort
    if title_base_url or _provider_lookup is not None:
        prompt_enhancer = HttpPromptEnhancer(
            base_url=title_base_url,
            api_key=title_api_key,
            model_name=title_model,
            model_resolver=model_resolver,
            ssl_verify=container.settings.ssl_verify,
            ssl_verify_provider=_ssl_verify_provider,  # live global SSL toggle
        )
    else:
        prompt_enhancer = OfflinePromptEnhancer()

    # ----- Real adapters (A3) -----
    # ``context_compressor`` was already built above (so the sub-agent loop
    # could reuse the same instance); the use case below reuses it too.

    # ----- MCP (Model Context Protocol) server registry -----
    # Owns MCP server config persistence (``<data>/config/mcp_servers.json``) +
    # connection lifecycle, and registers a connected server's tools onto the
    # SAME ``tools`` registry the built-in tools use — so MCP tools are
    # advertised + invoked through the identical pipeline (no streaming change).
    # Secure-by-default: the ``chat.chat_mcp_enabled`` Settings gate (default
    # False) must be truthy for the registry to connect / advertise tools.
    _mcp_enabled = bool(getattr(chat_settings, "chat_mcp_enabled", False))
    _mcp_config_path = container.data_paths.root / "config" / "mcp_servers.json"
    mcp_server_registry: McpServerRegistryPort = McpServerRegistry(
        tools=tools,
        config_path=_mcp_config_path,
        enabled=_mcp_enabled,
        secret_store=getattr(container, "secret_store", None),
        ssl_verify=container.settings.ssl_verify,
        ssl_verify_provider=_ssl_verify_provider,
        edition=getattr(container.settings, "edition", "internal"),
    )

    # Promote-ready turn-end detection (migration 057): adapt App Builder's
    # ImportScanBinsUseCase onto the chat PromoteReadyScanPort so the streaming
    # use case can persist Conversation.detected_model at turn end WITHOUT chat
    # importing qai.app_builder (context-isolation). The adapter holds the
    # container and resolves ``container.app_builder.import_scan_bins_use_case``
    # LAZILY at scan time — chat is built before app_builder in Container._wire,
    # but a real turn ends long after the whole container is wired. Local import
    # avoids an apps.api.di ↔ _chat_di circular import at module load.
    from apps.api._promote_ready_scan_bridge import (
        AppBuilderPromoteReadyScanAdapter,
    )

    _promote_ready_scan = AppBuilderPromoteReadyScanAdapter(container)

    # P5 (CONTEXT-COMPRESSION-NEXT v3.1 §7.2): construct the stream use case
    # FIRST so its :class:`CompactionCheckpointEngine` (the process-singleton
    # counter store) can be handed to :class:`GetContextDecisionUseCase` below. The
    # two use cases MUST share the same engine instance — otherwise the
    # ``escalation`` field on GET /context would read a cold, empty counter
    # and never surface the "migrate" entry the streaming path is actually
    # observing.

    # ONE reader instance, shared by the streaming gate below and by
    # ``ChatServices.computer_enabled`` (which ``GET /api/chat/tools`` reads).
    # Building it twice would work but invites the two surfaces to drift apart —
    # which is exactly the class of bug that let the panels advertise
    # ``computer`` while the model could never receive it.
    _computer_enabled_reader = _build_computer_enabled_reader(container)

    stream_chat_use_case = StreamChatUseCase(
        conversations=conversations,
        tabs=tabs,
        llm=llm,
        tools=tools,
        abort_registry=abort_registry,
        clock=clock,
        ids=ids,
        events=events,
        retry_policy=retry_policy,
        guardrail_factory=_guardrail_factory,
        tool_result_truncator=tool_result_truncator,
        system_prompt_builder=system_prompt_builder,
        context_compressor=context_compressor,
        prompt_snapshot_store=prompt_snapshot_store,
        provider_cache_registry=provider_cache_registry,
        budget_tracker=enforcement_tracker,
        budget_raise_pct=int(
            getattr(chat_settings, "chat_budget_raise_pct", 20)
        ),
        agent_event_stream=agent_handler,
        # The ONE spawn path: an ``agent`` tool_call is admitted here and runs
        # in the background, so the parent turn is never blocked on a child.
        sub_agent_bridge=sub_agent_bridge,
        code_persona_resolver=_code_persona_resolver,
        max_followup_rounds=-1,
        turn_warning_provider=_build_turn_warning_reader(container),
        hook_engine=hook_engine,
        local_skill_catalog_provider=_local_skill_catalog_provider,
        sub_agent_sessions=subagent_sessions,
        runtime_debug_config=_build_runtime_debug_config_reader(container),
        computer_enabled_provider=_computer_enabled_reader,
        cloud_image_config_provider=_build_cloud_image_reader(container),
        compaction_ratio_provider=_build_compaction_ratio_reader(container),
        image_upload_store=image_upload_store,
        injection_registry=injection_registry,
        # Exec 4-way race (Batch 5): the use case passes a TAB-BOUND
        # ``subscribe`` closure down through the soft-steer context so a
        # running ``exec`` can add the injection signal as a race arm
        # without importing any ``qai.chat`` symbol (cross-context contract).
        injection_signal_bridge=injection_signal_bridge,
        background_process_manager=None,
        # M5 full alignment: async reader — a Settings UI write to
        # forge_config.chat.bash_auto_background_enabled /
        # bash_auto_background_threshold_ms lands on the next turn WITHOUT a
        # restart.  The reader returns None when the switch is off or the
        # keys are absent/unreadable; the wrapper then applies the static
        # ChatSettings.bash_auto_background_* configured in
        # factory/config/server.toml, and a None reaching the use case makes
        # the exec race drop its threshold arm (Stop / injection unaffected).
        auto_bg_threshold_ms=_wrap_with_settings_fallback(
            _build_auto_bg_threshold_reader(container),
            chat_settings,
        ),
        tool_concurrency=tool_concurrency,
        compaction_checkpoint_store=compaction_checkpoint_store,
        promote_ready_scan=_promote_ready_scan,
        journal=compaction_journal,
        refresh_digest_uc=refresh_digest_uc,
        summarize_turn_prefix_uc=summarize_turn_prefix_uc,
        conversation_write_queue=conversation_write_queue,
    )

    # ================================================================
    # §6.4 refactor — unified background-job-completion pipeline
    # ================================================================
    #
    # The coordinator owns per-tab pending-work state and, when a
    # notification lands on an idle tab, kicks a one-shot headless
    # follow-up turn on :class:`StreamChatUseCase`.  The dispatcher
    # subscribes to :class:`SubAgentSessionTerminated` and
    # :class:`BackgroundProcessUpdated` (filtered for terminal
    # statuses) — for each event it appends a SYSTEM_NOTICE row to
    # the parent conversation and calls
    # :meth:`AgentSessionCoordinator.notify`.  The coordinator's
    # ``is_tab_streaming`` predicate consults the abort registry, so
    # a notification landing during a live turn simply queues the
    # dedup key for the round-end pending-work check.  A ``tab_id ->
    # ConversationId`` resolver reads the current
    # :class:`TabSessionStorePort` snapshot; if the tab has since
    # closed we drop the event as non-actionable.  All three concerns
    # (streaming predicate, headless runner, tab→conv resolver) live
    # under DI so no coordinator/dispatcher code touches the
    # concrete port implementations.
    def _coord_is_tab_streaming(tab_id: TabId) -> bool:
        try:
            return abort_registry.is_streaming(tab_id)
        except Exception:  # noqa: BLE001
            return False

    async def _coord_headless_runner(
        tab_id: TabId,
        conversation_id: ConversationId,
        model_hint: str | None,
    ) -> "frozenset[str] | None":
        # Late-bound to ``stream_chat_use_case`` (already constructed
        # above by the time the coordinator's task runs).  Forwards the
        # third return value (SYSTEM_NOTICE dedup_keys observed on the
        # rebuilt wire) up to the coordinator so it can ack every key
        # the LLM's wire folded in — not just the ones the coordinator
        # already knew about at drain-iteration start.
        _text, _usage, observed = await stream_chat_use_case.run_headless_followup(
            tab_id=tab_id,
            conversation_id=conversation_id,
            model_hint=model_hint,
        )
        return observed

    agent_session_coordinator = AgentSessionCoordinator(
        headless_runner=_coord_headless_runner,
        is_tab_streaming=_coord_is_tab_streaming,
    )

    async def _dispatch_resolve_conv(tab_id: TabId) -> ConversationId | None:
        # Snapshot the tab's active conversation.  ``tabs.get`` may
        # raise TabNotFoundError if the tab has since closed — treat
        # that as "drop this notification".
        try:
            tab = await tabs.get(tab_id)
            return tab.conversation_id
        except Exception:  # noqa: BLE001
            return None

    # Dispatcher's ``conv_resolver`` may be sync or async — the
    # dispatcher's :meth:`_resolve_conversation_id` awaits the result
    # if it's awaitable.  ``tabs.get`` is async, so we pass a plain
    # async resolver.
    # User's globally-selected model — same rung the WebUI sends as
    # ``model_hint`` on a live turn.  Used by the dispatcher's
    # ``_resolve_model_hint`` ladder as the last non-``None`` fallback
    # before a conv with no assistant-model_id row would otherwise
    # yield ``None`` and route the headless follow-up through the
    # offline default (``[no LLM endpoint configured]``).
    async def _dispatcher_default_model_provider() -> str | None:
        prefs_ns = getattr(container, "user_prefs", None)
        if prefs_ns is None:
            return None
        load_uc = getattr(prefs_ns, "load_document_use_case", None)
        if load_uc is None:
            return None
        try:
            doc = await load_uc.execute("ui.preferences")
        except Exception:  # noqa: BLE001 — best-effort
            return None
        if not isinstance(doc, dict):
            return None
        mid = doc.get("selected_model_id")
        return mid if isinstance(mid, str) and mid else None

    background_job_dispatcher = BackgroundJobDispatcher(
        bus=events,
        conversations=conversations,
        coordinator=agent_session_coordinator,
        ids=ids,
        conv_resolver=_dispatch_resolve_conv,
        default_model_provider=_dispatcher_default_model_provider,
        # Batch 5 (N.27): mirror every background bash job into the async-job
        # ledger.  The dispatcher is the right home: it already observes the
        # manager's running + terminal transitions AND owns the tab→conv
        # resolution the ledger's conversation-scoped ``owner_id`` needs
        # (the platform manager only knows a ``session_id``).
        async_job_manager=async_job_manager,
        # The SYSTEM_NOTICE append shares the streaming turn's per-conversation
        # worker, and its ack future is what the coordinator's
        # ``settle_pending`` waits on before a live turn folds the notice into
        # its wire.
        write_queue=conversation_write_queue,
        # Conversation -> live tab lookup for background-process completions.
        # A bgp record carries the CONVERSATION id (it must outlive the tab
        # that started it), while the coordinator wakes work by TAB — deriving
        # the tab from that id instead of looking it up produced a phantom tab
        # and a ``chat.tab_not_found`` follow-up that never woke the model.
        tab_sessions=tabs,
    )

    # Late-bind the dispatcher onto the ``sub_agent`` tool handler.
    # Construction order dictates this two-step: the handler is
    # registered above (before ``stream_chat_use_case`` / the
    # coordinator exist), so the dispatcher can only be threaded
    # into it now, after both are built.  The handler reads
    # ``self._dispatcher`` at call time and defensively no-ops when
    # it is None (unit stubs), so this write is a pure enrichment.
    _sub_agent_handler._dispatcher = background_job_dispatcher

    # Late-bind the coordinator onto ``stream_chat_use_case`` so
    # :meth:`_complete_turn` / :meth:`_release_streaming_tab` fire
    # :meth:`AgentSessionCoordinator.on_tab_idle` on turn end.  This
    # is the seam that drains SYSTEM_NOTICE dedup keys queued during
    # a live turn (``coord.notify.queued_for_live_turn``) — without
    # this the queued keys would stay in pending forever until a
    # NEW notification arrives on the same tab.
    stream_chat_use_case._agent_session_coordinator = agent_session_coordinator
    # Late-bind the ChatStreamBroadcaster so
    # :meth:`StreamChatUseCase.run_headless_followup` can publish
    # every headless-turn frame to it.  The primary chat WS handler
    # already registers + publishes to this broadcaster for
    # user-initiated turns; extending it to headless turns means
    # the frontend can attach a mid-turn ``/active-runs/{tab}/ws``
    # to a headless follow-up and see frames live — without this,
    # the user only sees the persisted assistant message on a
    # history reload.
    stream_chat_use_case._chat_stream_broadcaster = chat_stream_broadcaster

    # Late-bind the scheduled-task runner now that ``stream_chat_use_case``
    # exists (its construction is below the scheduling wiring above). The
    # scheduler's ``executor`` closure reads this holder at tick time. New
    # dependencies: the chat turn use case (drives one native agent turn) and
    # the shared abort registry (busy guard — skip a fire on a live tab).
    if scheduling_scheduler is not None:
        # Lazy provider of the CURRENTLY-ENABLED skill ids — the SAME set the
        # Skill panel (``GET /api/skills``) shows as enabled. Used to invert a
        # task's per-run skill whitelist into the turn's ``disabled_skills``.
        #
        # Source: ``user_prefs.list_skills_use_case`` — scans the ``skills/``
        # directory and merges each skill's 4-state mode from
        # ``forge.config skills.overrides`` (``off`` = disabled, else enabled),
        # exactly like the panel. (NOT the ai_coding ``SkillRegistryPort``
        # seam, which is a different, usually-empty registry.) Read at tick
        # time (long after startup) so ``container.user_prefs`` — built after
        # ``chat`` in the DI order — is present; a lazy deref avoids the
        # build-order forward reference. Best-effort: any failure yields an
        # empty catalog (no skill restriction) rather than breaking the run.
        async def _enabled_skills_provider() -> list[str]:
            prefs = getattr(container, "user_prefs", None)
            uc = getattr(prefs, "list_skills_use_case", None) if prefs else None
            if uc is None:
                return []
            result = await uc.execute()
            skills = result.get("skills", []) if isinstance(result, dict) else []
            return [
                str(s["skill_id"])
                for s in skills
                if isinstance(s, dict)
                and s.get("enabled") is True
                and s.get("skill_id")
            ]

        # Global tasks (no bound conversation) run in a DEDICATED system
        # conversation, get-or-created deterministically from the task id so
        # every run of the same global task lands in the same conversation
        # (which doubles as its run history). No writeback / no extra column.
        async def _global_session_provider(
            task: "ScheduledTask",
        ) -> tuple[ConversationId, TabId]:
            from qai.chat.domain.conversation import Conversation
            from qai.chat.domain.tab import ConversationTab
            from qai.chat.domain.ids import ConversationId, TabId

            conv_id = ConversationId.of(f"schedtask-{task.task_id}")
            tab_id = TabId.of(f"schedtab-{task.task_id}")
            existing = await conversations.find(conv_id)
            if existing is None:
                title = task.display_name or "定时任务"
                conv = Conversation.create(
                    conversation_id=conv_id,
                    title=f"⏰ {title}",
                    now=clock.now(),
                )
                await conversations.save(conv)
                tab = ConversationTab.open(
                    tab_id=tab_id,
                    conversation_id=conv_id,
                    now=clock.now(),
                )
                await tabs.save(tab)
            return conv_id, tab_id

        _sched_runner_holder["runner"] = ScheduledTaskRunner(
            stream_chat_use_case=stream_chat_use_case,
            abort_registry=abort_registry,
            skill_catalog_provider=_enabled_skills_provider,
            global_session_provider=_global_session_provider,
        )

    services = ChatServices(
        create_conversation_use_case=CreateConversationUseCase(
            conversations=conversations,
            clock=clock,
            ids=ids,
            events=events,
        ),
        list_conversations_use_case=ListConversationsUseCase(
            conversations=conversations,
        ),
        get_conversation_messages_use_case=GetConversationMessagesUseCase(
            conversations=conversations,
        ),
        rename_conversation_use_case=RenameConversationUseCase(
            conversations=conversations,
            clock=clock,
            events=events,
        ),
        delete_conversation_use_case=DeleteConversationUseCase(
            conversations=conversations,
            clock=clock,
            events=events,
            # Cascade: stop + delete the conversation's scheduled tasks BEFORE
            # the conversation row is removed, so no task keeps firing against a
            # gone conversation. ``None`` when the scheduling feature is dark.
            on_delete=(
                (
                    lambda cid, _s=scheduling_scheduler: _s.remove_by_conversation(
                        cid.value
                    )
                )
                if scheduling_scheduler is not None
                else None
            ),
        ),
        context_decision_use_case=GetContextDecisionUseCase(
            conversations=conversations,
            # P5 (CONTEXT-COMPRESSION-NEXT v3.1 §7.2): share the SAME
            # compaction engine the streaming path drives, so
            # ``ContextDecisionResult.escalation`` reflects the live per-turn
            # mid-turn compaction counter. Constructed above at
            # ``stream_chat_use_case = ...``. Without this the field would
            # always be ``"ok"``.
            compaction_engine=stream_chat_use_case.compaction_engine,
        ),
        stream_chat_use_case=stream_chat_use_case,
        stop_chat_use_case=StopChatUseCase(
            abort_registry=abort_registry,
            # Runtime-defect fix: give Stop the sub-agent registry so a tab ⏹
            subagent_abort_registry=subagent_abort_registry,
        ),
        cancel_tool_use_case=CancelToolUseCase(
            # Per-call single-tool cancel shares the SAME abort registry so it
            # can reach the in-flight turn's handle by tab_id and mark ONE
            # call_id for cancellation (without aborting the turn).
            abort_registry=abort_registry,
        ),
        conversations=conversations,
        tabs=tabs,
        abort_registry=abort_registry,
        chat_stream_broadcaster=chat_stream_broadcaster,
        llm=llm,
        tools=tools,
        openai_model_lister=openai_model_lister,
        open_tab_use_case=OpenTabUseCase(
            tabs=tabs,
            conversations=conversations,
            clock=clock,
            ids=ids,
        ),
        close_tab_use_case=CloseTabUseCase(
            tabs=tabs,
            clock=clock,
        ),
        list_active_tabs_use_case=ListActiveTabsUseCase(
            tabs=tabs,
        ),
        guardrail_factory=_guardrail_factory,
        tool_result_truncator=tool_result_truncator,
        retry_policy=retry_policy,
        system_prompt_builder=system_prompt_builder,
        title_generator=title_generator,
        generate_title_use_case=GenerateTitleUseCase(
            conversations=conversations,
            title_generator=title_generator,
            clock=clock,
            events=events,
        ),
        image_upload_store=image_upload_store,
        prompt_enhancer=prompt_enhancer,
        prompt_snapshot_store=prompt_snapshot_store,
        upload_image_use_case=UploadImageUseCase(store=image_upload_store),
        enhance_prompt_use_case=EnhancePromptUseCase(enhancer=prompt_enhancer),
        get_prompt_snapshot_use_case=GetPromptSnapshotUseCase(
            store=prompt_snapshot_store,
        ),
        save_prompt_snapshot_use_case=SavePromptSnapshotUseCase(
            store=prompt_snapshot_store,
        ),
        model_resolver=model_resolver,
        context_compressor=context_compressor,
        runtime_limit_store=runtime_limit_store,
        question_registry=question_registry,
        injection_registry=injection_registry,
        injection_signal_bridge=injection_signal_bridge,
        scheduling_scheduler=scheduling_scheduler,
        scheduling_aside_bridge=scheduling_aside_bridge,
        scheduling_task_store=_sched_store_for_services,
        scheduling_tool_handler=_sched_tool_handler,
        agent_session_coordinator=agent_session_coordinator,
        background_job_dispatcher=background_job_dispatcher,
        async_job_manager=async_job_manager,
        sub_agent_bridge=sub_agent_bridge,
        conversation_write_queue=conversation_write_queue,
        set_conversation_workspace_use_case=SetConversationWorkspaceUseCase(
            conversations=conversations,
            clock=clock,
        ),
        set_conversation_pinned_use_case=SetConversationPinnedUseCase(
            conversations=conversations,
            clock=clock,
        ),
        set_conversation_favorite_use_case=SetConversationFavoriteUseCase(
            conversations=conversations,
            clock=clock,
        ),
        budget_tracker=budget_tracker,
        subagent_sessions=subagent_sessions,
        participants=participants,
        subagent_stream_broadcaster=subagent_stream_broadcaster,
        subagent_abort_registry=subagent_abort_registry,
        # ----- Multi-agent discussion (block 4) -----
        # The orchestrator reuses the SAME context-management collaborators the
        # main / sub-agent loops use (one compressor / truncator
        # 口径 across all three kernel consumers); it builds its own
        # ``SingleAgentTurnKernel`` from them. The SpeakerSelector strategy is
        # chosen per ``conversation.discussion.selector_mode`` INSIDE the use
        # case (manager → ManagerAgentSelector with the injected ``llm`` +
        # round-robin safety net; round_robin → RoundRobinSelector), so no
        # selector instance is wired here.
        #
        # Additive 2026-06-21 collaborators (judgement 1 — reuse over rebuild):
        # * ``system_prompt_builder`` lets each speaker see the same SKILL
        #   catalog / Python env / Few-shot the main agent sees (skip_identity
        #   so the QAI ModelBuilder identity intro is dropped — speakers are
        #   user-defined roles, not QAI);
        # * ``prompt_snapshot_store`` stamps a per-round ``request_id`` so the
        #   front-end 📄 button on every tool card / final bubble opens that
        #   round's exact wire (parity with single-assistant chat);
        # * ``abort_registry`` + ``abort_handle_factory`` make Stop work for
        #   the discussion loop (same registry the main + sub-agent + question
        #   tool all share — one tab id, one handle).
        orchestrate_discussion=OrchestrateDiscussionUseCase(
            llm=llm,
            tools=tools,
            conversations=conversations,
            participants=participants,
            clock=clock,
            ids=ids,
            compressor=context_compressor,
            truncator=tool_result_truncator,
            system_prompt_builder=system_prompt_builder,
            prompt_snapshot_store=prompt_snapshot_store,
            abort_registry=abort_registry,
            abort_handle_factory=AsyncioStreamAbortHandle,
            mode_templates=mode_templates,
            # Built-in template i18n (migration 056, method A): the agent +
            # roster template repos let the orchestrator re-resolve a built-in
            # participant's persona translation at runtime by (template_id +
            # locale). Reuse the SAME repos the template-management use cases
            # use (single source of truth).
            agent_templates=agent_templates,
            roster_templates=roster_templates,
            # DISC-2 P2-step1 (§22A.5): grey-zone LLM intent classifier reusing
            # the shared streaming ``llm`` port. Only consulted when the
            # per-conversation ``intent_classifier_enabled`` flag is on AND the
            # heuristic marked the message a grey-zone shape (default OFF →
            # pure-heuristic, zero LLM call).
            intent_classifier=LlmIntentClassifier(llm=llm),
            # Per-conversation TOKEN budget (max_budget_tokens): the SAME gated
            # ``enforcement_tracker`` the streaming use case + sub-agent handler
            # use, so a discussion's per-speaker token usage accumulates into the
            # SAME per-conversation budget pool — one session-level cap covers
            # the whole tree (main agent + sub-agents + every discussion
            # speaker). ``NullBudgetTracker`` when ``chat_budget_enabled`` is off.
            budget_tracker=enforcement_tracker,
        ),
        list_participants_use_case=ListParticipantsUseCase(
            participants=participants,
        ),
        create_participant_use_case=CreateParticipantUseCase(
            participants=participants,
            conversations=conversations,
            clock=clock,
            ids=ids,
        ),
        update_participant_use_case=UpdateParticipantUseCase(
            participants=participants,
            clock=clock,
        ),
        delete_participant_use_case=DeleteParticipantUseCase(
            participants=participants,
        ),
        get_discussion_config_use_case=GetDiscussionConfigUseCase(
            conversations=conversations,
        ),
        set_discussion_config_use_case=SetDiscussionConfigUseCase(
            conversations=conversations,
            clock=clock,
        ),
        get_implementation_plan_use_case=GetImplementationPlanUseCase(
            conversations=conversations,
        ),
        update_implementation_plan_use_case=UpdateImplementationPlanUseCase(
            conversations=conversations,
            participants=participants,
            clock=clock,
        ),
        # DISC-1 二期-step2: LLM feature-item extractor + planner use case.
        # The extractor reuses the SAME provider-routing LLM stream the discussion
        # loop uses (one transport for all LLM calls).
        generate_implementation_plan_use_case=GenerateImplementationPlanUseCase(
            conversations=conversations,
            participants=participants,
            planner=OrchestratePlanningUseCase(
                extractor=LlmFeatureItemExtractor(llm=llm),
                ids=ids,
                clock=clock,
            ),
            clock=clock,
        ),
        list_roster_templates_use_case=ListRosterTemplatesUseCase(
            templates=roster_templates,
        ),
        create_roster_template_use_case=CreateRosterTemplateUseCase(
            templates=roster_templates,
            clock=clock,
            ids=ids,
        ),
        update_roster_template_use_case=UpdateRosterTemplateUseCase(
            templates=roster_templates,
            clock=clock,
        ),
        delete_roster_template_use_case=DeleteRosterTemplateUseCase(
            templates=roster_templates,
        ),
        apply_roster_template_use_case=ApplyRosterTemplateUseCase(
            templates=roster_templates,
            participants=participants,
            conversations=conversations,
            mode_templates=mode_templates,
            clock=clock,
            ids=ids,
        ),
        clone_roster_template_use_case=CloneRosterTemplateUseCase(
            templates=roster_templates,
            clock=clock,
            ids=ids,
        ),
        reset_roster_template_use_case=ResetRosterTemplateUseCase(
            templates=roster_templates,
            clock=clock,
        ),
        list_agent_templates_use_case=ListAgentTemplatesUseCase(
            templates=agent_templates,
        ),
        create_agent_template_use_case=CreateAgentTemplateUseCase(
            templates=agent_templates,
            clock=clock,
            ids=ids,
        ),
        update_agent_template_use_case=UpdateAgentTemplateUseCase(
            templates=agent_templates,
            clock=clock,
        ),
        delete_agent_template_use_case=DeleteAgentTemplateUseCase(
            templates=agent_templates,
        ),
        apply_agent_template_use_case=ApplyAgentTemplateUseCase(
            templates=agent_templates,
            participants=participants,
            conversations=conversations,
            clock=clock,
            ids=ids,
        ),
        clone_agent_template_use_case=CloneAgentTemplateUseCase(
            templates=agent_templates,
            clock=clock,
            ids=ids,
        ),
        reset_agent_template_use_case=ResetAgentTemplateUseCase(
            templates=agent_templates,
            clock=clock,
        ),
        list_mode_templates_use_case=ListModeTemplatesUseCase(
            templates=mode_templates,
        ),
        create_mode_template_use_case=CreateModeTemplateUseCase(
            templates=mode_templates,
            clock=clock,
            ids=ids,
        ),
        update_mode_template_use_case=UpdateModeTemplateUseCase(
            templates=mode_templates,
            clock=clock,
        ),
        delete_mode_template_use_case=DeleteModeTemplateUseCase(
            templates=mode_templates,
            conversations=conversations,
            clock=clock,
        ),
        apply_mode_template_use_case=ApplyModeTemplateUseCase(
            templates=mode_templates,
            conversations=conversations,
            clock=clock,
        ),
        count_mode_template_usage_use_case=CountModeTemplateUsageUseCase(
            conversations=conversations,
        ),
        clone_mode_template_use_case=CloneModeTemplateUseCase(
            templates=mode_templates,
            clock=clock,
            ids=ids,
        ),
        reset_mode_template_use_case=ResetModeTemplateUseCase(
            templates=mode_templates,
            clock=clock,
        ),
        path_lock=path_lock,
        mcp_server_registry=mcp_server_registry,
        manage_mcp_servers_use_case=ManageMcpServersUseCase(
            registry=mcp_server_registry,
        ),
        fork_conversation_use_case=ForkConversationUseCase(
            conversations=conversations,
            clock=clock,
            ids=ids,
        ),
        # P5 (CONTEXT-COMPRESSION-NEXT v3.1 §7.2): the handoff escape hatch.
        # Shares the SAME compaction engine the streaming / force paths drive
        # so a migration issued right after ``/compact`` can WAIT for that
        # pass's background digest task instead of racing it: the reply lands
        # in milliseconds, the summary 20-40s later. Without the engine the
        # use case still works — it just falls straight through to the
        # degraded handoff (verbatim conversation tail) when no digest exists.
        migrate_conversation_use_case=MigrateConversationUseCase(
            conversations=conversations,
            checkpoint_store=compaction_checkpoint_store,
            ids=ids,
            clock=clock,
            compaction_engine=stream_chat_use_case.compaction_engine,
        ),
        # Task J — force-compact command + expose the checkpoint store so the
        # channel bridge's ``/compact clear`` can drop the persisted digest /
        # ledger. Shares the SAME compaction engine the streaming path drives
        # so a forced pass is byte-for-byte the same as an auto-trigger.
        force_compact_chat_use_case=ForceCompactChatUseCase(
            conversations=conversations,
            compaction_engine=stream_chat_use_case.compaction_engine,
        ),
        compaction_checkpoint_store=compaction_checkpoint_store,
        # Same reader the streaming gate uses, so the advertised tool set and
        # the enforced tool set can never disagree.
        computer_enabled=_computer_enabled_reader,
    )
    return services

# ---------------------------------------------------------------------------
# LLM-stream wiring helper
# ---------------------------------------------------------------------------


def _build_llm_stream(
    *,
    container: Container,
    runtime_limit_store: RuntimeLimitStorePort | None = None,
) -> HttpOpenAICompatibleLLMStream:
    """Construct the OpenAI-compatible HTTP stream adapter.

    Pulls ``base_url`` / ``api_key`` / ``model`` from
    ``container.settings.chat`` if present; falls back to the offline
    notice mode (no upstream URL) so unit / integration tests that do
    not configure the chat settings still run cleanly.

    ``runtime_limit_store`` is the D2 :class:`RuntimeLimitStorePort`
    process singleton holding the runtime-learned ``max_tokens`` ceilings;
    the adapter records / reads them through it instead of an
    infrastructure module global.  When omitted (legacy callers / tests)
    the adapter falls back to a per-instance store.
    """
    chat_settings = getattr(container.settings, "chat", None)
    base_url = getattr(chat_settings, "llm_base_url", None) or None
    api_key = getattr(chat_settings, "llm_api_key", None) or None
    model = getattr(chat_settings, "llm_default_model", None) or "qai-default"
    timeout_seconds = _resolve_cloud_stream_timeout(chat_settings)
    from apps.api._global_proxy import build_ssl_verify_provider

    return HttpOpenAICompatibleLLMStream(
        base_url=base_url,
        api_key=api_key,
        model=model,
        ids=container.ids,
        timeout_seconds=timeout_seconds,
        runtime_limit_store=runtime_limit_store,
        ssl_verify=container.settings.ssl_verify,
        # Live provider so the default stream's httpx client reads the global
        # SSL toggle at build time (hot-applies); frozen bool is back-compat.
        ssl_verify_provider=build_ssl_verify_provider(container),
    )


# Fallback for the cloud stream base timeout when ``ChatSettings`` (or its
# ``llm_stream_timeout_seconds`` field) is unavailable — kept in sync with the
# ``ChatSettings.llm_stream_timeout_seconds`` field default (120s, aligned to
# V1 ``cloud_shared.timeout_seconds``).  Defined here (not imported from the
# infrastructure module's private ``_DEFAULT_TIMEOUT_SECONDS``) so the wiring
# layer does not reach into another module's private constants.
_CLOUD_STREAM_TIMEOUT_FALLBACK: float = 120.0


def _resolve_cloud_stream_timeout(chat_settings: Any) -> float:
    """Resolve the cloud stream base timeout (connect/write/pool) seconds.

    Reads ``ChatSettings.llm_stream_timeout_seconds`` (default 120, aligned to
    V1 ``cloud_shared.timeout_seconds``).  The adapter derives the long SSE
    *read* (idle-gap) timeout from this as ``base * 5`` (= 600s by default), so
    a model may pause that long between tokens — e.g. while organising a long
    tool-call argument — without being cut off.  Falls back to
    ``_CLOUD_STREAM_TIMEOUT_FALLBACK`` (mirrors the ``ChatSettings`` field
    default) when the setting is absent / invalid (legacy callers / tests).
    """
    value = getattr(chat_settings, "llm_stream_timeout_seconds", None)
    if value is None:
        return _CLOUD_STREAM_TIMEOUT_FALLBACK
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return _CLOUD_STREAM_TIMEOUT_FALLBACK
    return seconds if seconds >= 1.0 else _CLOUD_STREAM_TIMEOUT_FALLBACK


def wire_ai_coding_tools_into_chat(
    *,
    chat: ChatServices,
    ai_coding: Any,
) -> None:
    """Post-build hook: register ai_coding tools onto the chat tool registry.

    ``build_chat_services`` runs BEFORE ``build_ai_coding_services`` in the
    DI construction order (``apps/api/di.py``), so ``container.ai_coding``
    is ``None`` when the chat context is first wired.  This function is
    called by ``di.py`` immediately after ``ai_coding`` is built, so the
    10 production tools (read / list / write / edit / glob / grep / exec /
    web_fetch / apply_patch / appbuilder_run) are registered with their OpenAI
    function-calling schemas before the first chat turn arrives.

    Note: the ``appbuilder_run`` registered here is the ai_coding *stub*
    (``appbuilder_not_wired``); ``wire_appbuilder_tools_into_chat`` (called
    later, after the app_builder context is built) OVERWRITES it with a real
    inference handler and ADDS ``appbuilder_batch_run``.

    Without this post-build step ``RegistryBackedToolInvocation.schemas()``
    returns an empty tuple, ``tools_schemas`` is never injected into
    ``LLMStreamRequest.extra``, and cloud LLMs never receive the standard
    ``payload["tools"]`` array — they fall back to the system-prompt
    ``<tools>`` XML block and emit raw XML ``<function_calls>`` text instead
    of native ``tool_calls`` deltas.

    PR-fix-cloud-tools-di (2026-06-05): root-cause fix for the P0-①
    regression where cloud Claude received no function-calling schema.
    """
    from qai.chat.adapters import RegistryBackedToolInvocation

    tools = chat.tools
    if not isinstance(tools, RegistryBackedToolInvocation):
        # Defensive: if the tools port is not the registry-backed adapter
        # (e.g. a test stub), skip silently — the stub handles its own
        # schema advertisement.
        return

    file_guard = getattr(ai_coding, "file_guard", None)
    file_broker = getattr(ai_coding, "file_broker", None)
    if file_guard is None or file_broker is None:
        return

    from ._chat_tool_bridge import register_ai_coding_tools_into_chat

    register_ai_coding_tools_into_chat(
        tools=tools,
        file_guard=file_guard,
        file_broker=file_broker,
        tool_result_store=getattr(ai_coding, "tool_result_store", None),
        path_lock=getattr(chat, "path_lock", None),
    )


def wire_tool_result_store_into_chat(
    *,
    chat: ChatServices,
    ai_coding: Any,
) -> None:
    """Post-build hook: give the chat truncator the ai_coding overflow store.

    Same shape and same reason as :func:`wire_ai_coding_tools_into_chat`:
    ``build_chat_services`` runs BEFORE ``build_ai_coding_services`` in the DI
    construction order (``apps/api/di.py:_build_contexts``) and
    ``Container.ai_coding`` is declared ``field(init=False)``, so the attribute
    does not exist while the chat context is being wired. The truncator is
    therefore built with ``overflow_store=None`` and back-filled here, once
    ``ai_coding`` really exists.

    WHY THIS EXISTS: without the back-fill the truncator kept
    ``overflow_store=None`` for the whole process lifetime, so
    ``AdaptiveToolResultTruncator._persist_overflow`` took its
    ``OVERFLOW_NOT_INJECTED`` branch on every over-budget result — head+tail
    truncation with the middle dropped IRRECOVERABLY, for every tool except
    the self-managing ones (exec / grep / glob persist their own bodies). The
    only symptom was one INFO line per truncation; the startup self-check
    reported ``artifact_overflow_store_injected=False`` and nothing else broke,
    which is exactly how the feature stayed dark after shipping.

    Assignment is IN PLACE on the existing truncator instance
    (:class:`AdaptiveToolResultTruncator` is a NON-frozen dataclass): the same
    object is already referenced by ``StreamChatUseCase``, the sub-agent
    handler and the :class:`ChatServices` namespace, so constructing a
    replacement would leave those three holding a store-less copy.

    Best-effort and idempotent: a missing field / non-adaptive truncator (test
    stub) leaves the pure-truncation behaviour untouched. This hook emits the
    AUTHORITATIVE ``chat.di.tool_result_overflow_store`` witness — it runs
    after the store can actually be resolved, so a ``False`` here is a real
    wiring failure and is logged at WARNING.
    """
    truncator = getattr(chat, "tool_result_truncator", None)
    store = getattr(ai_coding, "tool_result_store", None)
    # ``overflow_store`` must be a real field on the truncator; a test stub
    # without it is left alone rather than gaining a stray attribute.
    attachable = store is not None and hasattr(truncator, "overflow_store")
    if attachable:
        truncator.overflow_store = store
        _log.info(
            "chat.di.tool_result_overflow_store",
            injected=True,
            store_type=type(store).__name__,
            ordered_slice_cap=getattr(truncator, "ordered_slice_cap", None),
        )
    else:
        # A MISSING injection is its own silent-failure class: everything still
        # "works", the middle of every over-budget tool result is just gone
        # forever. WARNING (not INFO) because the expected state is True — see
        # docs/90-refactor/CONTEXT-COMPACTION.md §6.2.
        _log.warning(
            "chat.di.tool_result_overflow_store",
            injected=False,
            store_type=None,
            store_resolved=store is not None,
            truncator_type=type(truncator).__name__,
            ordered_slice_cap=getattr(truncator, "ordered_slice_cap", None),
        )


def wire_appbuilder_tools_into_chat(
    *,
    chat: ChatServices,
    app_builder: Any,
    ai_coding: Any = None,
) -> None:
    """Post-build hook: wire REAL App Builder inference into the chat tools.

    ``build_chat_services`` (and the ai_coding wire) run BEFORE
    ``build_app_builder_services`` in the DI order (``apps/api/di.py``), so
    the chat registry initially carries only the ai_coding ``appbuilder_run``
    *stub* (``appbuilder_not_wired``) and no ``appbuilder_batch_run`` at all.

    This function is called by ``di.py`` immediately after ``app_builder`` is
    built; it OVERWRITES ``appbuilder_run`` with a real handler backed by
    ``app_builder.run_app_use_case`` and ADDS ``appbuilder_batch_run`` —
    restoring the V1 LLM Agent Pipeline tools (audit ``D6 §6.11`` / ``§6.12``).

    Cross-context discipline: the bridge lives in ``apps/api/`` (the only
    layer allowed to compose two contexts); ``qai.chat`` never imports
    ``qai.app_builder``. ``ai_coding`` (optional) supplies the SAME
    ``FileGuardPort`` the file tools use so an out-of-policy inputs path is
    gated exactly like ``read``.

    Best-effort: any failure (app_builder unwired / use case missing) leaves
    the ai_coding stub in place so chat startup never breaks.
    """
    from qai.chat.adapters import RegistryBackedToolInvocation

    tools = chat.tools
    if not isinstance(tools, RegistryBackedToolInvocation):
        return
    run_app_use_case = getattr(app_builder, "run_app_use_case", None)
    if run_app_use_case is None:
        return

    # Optional friendly "available models" hint source — the enabled app
    # models, keyed by id. Resolved lazily per call so a runtime install/enable
    # is reflected without a restart.
    list_uc = getattr(app_builder, "list_app_models_use_case", None)

    async def _app_model_lookup() -> dict[str, str]:
        if list_uc is None:
            return {}
        try:
            models = await list_uc.execute(include_disabled=False)
        except Exception:  # noqa: BLE001 — hint only; never raise
            return {}
        out: dict[str, str] = {}
        for m in models:
            try:
                out[str(m.id)] = str(getattr(m, "title", "") or m.id)
            except Exception:  # noqa: BLE001
                continue
        return out

    from ._chat_appbuilder_tool_bridge import (
        register_appbuilder_tools_into_chat,
    )

    register_appbuilder_tools_into_chat(
        tools=tools,
        run_app_use_case=run_app_use_case,
        app_model_lookup=_app_model_lookup,
        file_guard=getattr(ai_coding, "file_guard", None),
    )


def wire_web_search_tool_into_chat(
    *,
    chat: ChatServices,
    container: Any,
    ai_coding: Any = None,
) -> None:
    """Post-build hook: wire the ``web_search`` tool into chat.

    Builds the pluggable :class:`SearchProviderRegistry` at the DI seam: the
    multi-engine ``web`` provider on BOTH editions, plus the intranet CEBot
    provider only when ``container.settings.is_internal``. The registry is
    ``None`` (and ``web_search`` unregistered) only in the extreme case where
    even ``web`` fails to construct; otherwise the tool exists under both
    editions (parity with how the conditional App Builder tools are wired).

    Cross-context discipline: the bridge lives in ``apps/api`` (the only layer
    allowed to compose contexts); ``qai.chat`` never imports
    ``qai.platform.web_search``. The ai_coding ``FileGuardPort`` (when
    available) is passed for signature parity (web_search performs no
    filesystem access).

    Best-effort: any failure leaves the chat registry unchanged so chat
    startup never breaks.
    """
    from qai.chat.adapters import RegistryBackedToolInvocation

    tools = chat.tools
    if not isinstance(tools, RegistryBackedToolInvocation):
        return

    from ._web_search_bridge import build_search_registry

    search_registry = build_search_registry(container=container)
    if search_registry is None:
        return

    # Install the unified Settings.ssl_verify into the independent search
    # engines' HTTP client seam (same pattern as the web_fetch tool): the
    # internal build runs behind an enterprise TLS-intercepting gateway whose
    # MITM CA the default trust store rejects, so the engines must follow the
    # global verify switch or every HTTPS fetch fails CERTIFICATE_VERIFY_FAILED.
    try:
        from qai.platform.web_search.independent.http_client import (
            set_search_ssl_verify,
        )

        set_search_ssl_verify(
            bool(getattr(container.settings, "ssl_verify", True))
        )
    except Exception:  # noqa: BLE001, S110 — best-effort; never break chat startup
        pass

    # Wire the global proxy into the search HTTP client seam.
    #
    # The proxy the user sets in Settings → Proxy is written by
    # ``apply_tools_runtime_config`` into the web_fetch tool's ``_CONFIG``
    # (via ``set_global_proxy``), NOT into ``settings.tools.global_proxy``.
    # So the provider must read ``get_global_proxy()`` from the web_fetch
    # handler — that is the single live source of truth for the proxy URL
    # at runtime, and it is updated immediately when the user changes the
    # setting without a restart.
    try:
        from qai.ai_coding.infrastructure.tools.handlers.web import (
            get_global_proxy as _get_web_fetch_proxy,
        )
        from qai.platform.web_search.independent.http_client import (
            set_search_proxy_provider,
        )
        import logging as _logging

        # The provider is a zero-argument callable called at every new client
        # build, so it always reflects the current proxy setting.
        set_search_proxy_provider(_get_web_fetch_proxy)

        _log = _logging.getLogger("web_search.proxy_init")
        _log.info(
            "web_search.proxy_init: proxy=%s ssl_verify=%s",
            _get_web_fetch_proxy() or "(none — direct connection)",
            bool(getattr(getattr(container, "settings", None), "ssl_verify", True)),
        )
    except Exception:  # noqa: BLE001, S110 — best-effort; never break chat startup
        pass

    from ._chat_web_search_tool_bridge import register_web_search_tool_into_chat

    register_web_search_tool_into_chat(
        tools=tools,
        search_registry=search_registry,
        search_config_reader=_build_search_config_reader(container),
        file_guard=getattr(ai_coding, "file_guard", None),
    )


def wire_browser_tool_into_chat(
    *,
    chat: ChatServices,
    container: Any,
) -> None:
    """Post-build hook: wire the conditional ``browser`` tool into chat.

    Builds a process-lifetime :class:`BrowserSessionManager` (gated on
    Playwright being installed + the ``[browser]`` config), stashes it on the
    container as ``browser_manager`` so the application lifespan can ``aclose``
    it on shutdown, and registers the ``browser`` tool onto the chat registry.
    Best-effort: never blocks daemon startup. When Playwright is absent the
    manager is ``None`` and the tool stays unregistered.
    """
    from qai.chat.adapters import RegistryBackedToolInvocation

    tools = chat.tools
    if not isinstance(tools, RegistryBackedToolInvocation):
        return

    from ._browser_di import build_browser_manager

    manager = build_browser_manager(container=container)
    if manager is None:
        return

    # Stash for lifespan shutdown (aclose) — process-lifetime singleton.
    try:
        container.browser_manager = manager
    except Exception:  # noqa: BLE001, S110 — best-effort; never break startup
        pass

    from ._chat_browser_tool_bridge import register_browser_tool_into_chat

    register_browser_tool_into_chat(tools=tools, browser_manager=manager)


def _build_search_config_reader(container: Any) -> Any:
    """Per-turn reader of ``forge_config.chat.search`` (mode / default_provider).

    Returns a zero-arg ASYNC callable that RE-READS the ``chat.search`` dict
    from the ``forge.config`` user_prefs KV document on every call — the SAME
    store the Settings panel ``POST /api/forge-config`` writes to. Re-reading
    per call (not cached) is intentional: flipping the search-source toggle
    takes effect on the NEXT turn WITHOUT a restart.

    Previous implementation read ``<data>/config/forge_config.json`` (the file),
    which is NOT updated by ``POST /api/forge-config`` (that writes to KV). This
    caused the user's search-mode choice to be silently ignored (bug: always
    "both" regardless of setting).
    """

    async def _read() -> dict[str, Any]:
        try:
            up = getattr(container, "user_prefs", None)
            if up is None:
                return {}
            load_uc = getattr(up, "load_document_use_case", None)
            if load_uc is None:
                return {}
            doc = await load_uc.execute("forge.config")
            chat_cfg = (doc or {}).get("chat")
            if not isinstance(chat_cfg, dict):
                return {}
            search_cfg = chat_cfg.get("search")
            return search_cfg if isinstance(search_cfg, dict) else {}
        except Exception:  # noqa: BLE001 — routing policy is opt-in; never break
            return {}

    return _read


__all__ = [
    "ChatServices",
    "build_chat_services",
    "wire_ai_coding_tools_into_chat",
    "wire_tool_result_store_into_chat",
    "wire_appbuilder_tools_into_chat",
    "wire_web_search_tool_into_chat",
    "wire_browser_tool_into_chat",
]
