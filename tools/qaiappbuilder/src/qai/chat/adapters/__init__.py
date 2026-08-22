# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Public API for ``qai.chat.adapters`` (PR-042 + PR-043 + PR-401a + PR-401b).

PR-042 retired 6 of 7 in-memory ``_Fake<Port>`` adapters from
``apps/api/_chat_di.py``; PR-043 retires the final one
(``_FakeTabSessionStore``) by adding :class:`SqliteTabSessionStore`
together with the 3 missing tab use cases (``Open/Close/ListActiveTabs``).
After PR-043 the chat context has zero in-memory fakes in production
DI wiring.

PR-401a (S7.5 lane L4) added two pure-function modules migrated from
the legacy ``backend/chat_handler.py``:

* :mod:`qai.chat.adapters.openai_protocol` — tool-call parsers,
  streaming buffer slicer, tool-name / tool-message structural
  sanitisers, Vertex AI thought-signature flattener, ANSI escape
  stripper.
* :mod:`qai.chat.adapters.error_classifier` — keyword-based fallback
  classifiers for prompt-too-long and throttling categories.

PR-401b (S7.5 lane L4) adds four agentic-loop adapters that
:class:`StreamChatUseCase` consumes via DI:

* :class:`InMemoryGuardrailController` — per-tool-call guardrails
  (port: :class:`qai.chat.application.ports.GuardrailPort`).
* :class:`AdaptiveToolResultTruncator` — model-family / pressure-aware
  tool-result truncation (port:
  :class:`qai.chat.application.ports.ToolResultTruncatorPort`).
* :class:`DefaultStreamRetryPolicy` — prompt-too-long + throttling
  retry schedules (port:
  :class:`qai.chat.application.ports.RetryPolicyPort`).
* :class:`StaticSystemPromptBuilder` and
  :class:`RichSystemPromptBuilder` — system-prompt assembly (port:
  :class:`qai.chat.application.ports.SystemPromptBuilderPort`); the
  static adapter ships a single configurable constant for tests and
  minimal deployments while the rich adapter materialises the full
  SKILL / persona / feature-mode catalogue for production wiring.
"""

from __future__ import annotations

from qai.chat.adapters import error_classifier, openai_protocol
from qai.chat.adapters.agent_template_repository import (
    SqliteAgentTemplateRepository,
)
from qai.chat.adapters.agent_tool import AgentToolHandler
from qai.chat.adapters.compaction_checkpoint_repository import (
    SqliteCompactionCheckpointRepository,
)
from qai.chat.adapters.context_compressor import (
    ThreeLevelContextCompressor,
)
from qai.chat.adapters.conversation_repository import (
    SqliteConversationRepository,
)
from qai.chat.adapters.conversation_search_repository import (
    SqliteConversationSearchRepository,
)
from qai.chat.adapters.experience_repository import (
    ExperienceRecallHit,
    SqliteExperienceRecall,
    SqliteExperienceRepository,
)
from qai.chat.adapters.experience_tools import (
    RECALL_EXPERIENCE_TOOL_SCHEMA,
    REMEMBER_EXPERIENCE_TOOL_SCHEMA,
    RecallExperienceToolHandler,
    RememberExperienceToolHandler,
)
from qai.chat.adapters.guardrail import InMemoryGuardrailController
from qai.chat.adapters.harness_tools import (
    IMPLEMENTATION_PLAN_TOOL_SCHEMA,
    QUESTION_TOOL_SCHEMA,
    SKILL_TOOL_SCHEMA,
    TODOWRITE_TOOL_SCHEMA,
    ImplementationPlanToolHandler,
    QuestionToolHandler,
    SkillToolHandler,
    TodoWriteToolHandler,
)
from qai.chat.adapters.image_upload_store import (
    URL_PREFIX as IMAGE_UPLOAD_URL_PREFIX,
    FileSystemImageUploadStore,
)
from qai.chat.adapters.injection_registry import InMemoryInjectionRegistry
from qai.chat.adapters.llm_feature_item_extractor import (
    LlmFeatureItemExtractor,
)
from qai.chat.adapters.llm_intent_classifier import (
    LlmIntentClassifier,
)
from qai.chat.adapters.llm_title_generator import (
    HttpLLMTitleGenerator,
    OfflineTitleGenerator,
)
from qai.chat.adapters.local_model_stream import LocalModelStreamAdapter
from qai.chat.adapters.mcp_client import (
    McpServerRegistry,
    McpToolInvocationAdapter,
)
from qai.chat.adapters.mode_template_repository import (
    SqliteModeTemplateRepository,
)
from qai.chat.adapters.model_resolver import (
    ProviderAwareModelResolver,
    SettingsBasedModelResolver,
)
from qai.chat.adapters.participant_repository import (
    SqliteParticipantRepository,
)
from qai.chat.adapters.prompt_enhancer import (
    HttpPromptEnhancer,
    OfflinePromptEnhancer,
)
from qai.chat.adapters.prompt_snapshot_store import (
    DEFAULT_SNAPSHOT_CAPACITY,
    InMemoryPromptSnapshotStore,
)
from qai.chat.adapters.question_registry import InMemoryQuestionRegistry
from qai.chat.adapters.retry_policy import DefaultStreamRetryPolicy
from qai.chat.adapters.roster_template_repository import (
    SqliteRosterTemplateRepository,
)
from qai.chat.adapters.runtime_limit_store import (
    InMemoryRuntimeLimitStore,
)
from qai.chat.adapters.search_conversations_tool import (
    SEARCH_CONVERSATIONS_TOOL_SCHEMA,
    SearchConversationsToolHandler,
)
from qai.chat.adapters.setting_resolver import DictSettingResolver
from qai.chat.adapters.stream_abort_registry import (
    InMemoryStreamAbortRegistry,
)
from qai.chat.adapters.sub_agent_abort_registry import (
    InMemorySubAgentAbortRegistry,
)
from qai.chat.adapters.sub_agent_session_repository import (
    QueuedSubAgentSessionRepository,
    SqliteSubAgentSessionRepository,
)
from qai.chat.adapters.system_prompt_builder import (
    RichSystemPromptBuilder,
    StaticSystemPromptBuilder,
)
from qai.chat.adapters.tab_session_store import (
    SqliteTabSessionStore,
)
from qai.chat.adapters.tool_invocation import (
    RegistryBackedToolInvocation,
    ToolHandler,
)
from qai.chat.adapters.tool_result_truncator import (
    AdaptiveToolResultTruncator,
    ordered_slice_cap_for,
)

__all__ = [
    "AgentToolHandler",
    "SqliteConversationRepository",
    "SqliteConversationSearchRepository",
    "SqliteExperienceRepository",
    "SqliteExperienceRecall",
    "ExperienceRecallHit",
    "RememberExperienceToolHandler",
    "RecallExperienceToolHandler",
    "REMEMBER_EXPERIENCE_TOOL_SCHEMA",
    "RECALL_EXPERIENCE_TOOL_SCHEMA",
    "SearchConversationsToolHandler",
    "SEARCH_CONVERSATIONS_TOOL_SCHEMA",
    "SqliteTabSessionStore",
    "RegistryBackedToolInvocation",
    "ToolHandler",
    "McpServerRegistry",
    "McpToolInvocationAdapter",
    "InMemoryStreamAbortRegistry",
    "InMemorySubAgentAbortRegistry",
    # harness control tools (V2 enhancement): todowrite + blocking question
    "InMemoryQuestionRegistry",
    # mid-turn user injection (V2 enhancement): inject button → inter-round seam
    "InMemoryInjectionRegistry",
    "TodoWriteToolHandler",
    "QuestionToolHandler",
    "SkillToolHandler",
    "ImplementationPlanToolHandler",
    "TODOWRITE_TOOL_SCHEMA",
    "QUESTION_TOOL_SCHEMA",
    "SKILL_TOOL_SCHEMA",
    "IMPLEMENTATION_PLAN_TOOL_SCHEMA",
    # PR-401a — pure-function helpers (modules; consumers import names directly)
    "openai_protocol",
    "error_classifier",
    # PR-401b — agentic-loop adapters
    "InMemoryGuardrailController",
    "AdaptiveToolResultTruncator",
    "ordered_slice_cap_for",
    "DefaultStreamRetryPolicy",
    "StaticSystemPromptBuilder",
    "RichSystemPromptBuilder",
    # hook engine + setting resolver (migrated from ai_coding agent harness)
    "DictSettingResolver",
    # PR-402 — title generation + memory recall
    "HttpLLMTitleGenerator",
    "OfflineTitleGenerator",
    # DISC-2 P2-step1 — grey-zone LLM intent classifier (§22A.5)
    "LlmIntentClassifier",
    # DISC-1 二期-step2 — LLM feature-item extractor (planner; §22.4)
    "LlmFeatureItemExtractor",
    # local on-device model streaming adapter (V1 _stream_local parity)
    "LocalModelStreamAdapter",
    # PR-403 — image upload + prompt enhance + prompt snapshot
    "FileSystemImageUploadStore",
    "IMAGE_UPLOAD_URL_PREFIX",
    "HttpPromptEnhancer",
    "OfflinePromptEnhancer",
    "InMemoryPromptSnapshotStore",
    "DEFAULT_SNAPSHOT_CAPACITY",
    # A3 — context compression
    "ThreeLevelContextCompressor",
    # CCD-5 — session compaction checkpoint persistence (durable backing store)
    "SqliteCompactionCheckpointRepository",
    # A4 — model resolution
    "SettingsBasedModelResolver",
    "ProviderAwareModelResolver",
    # D2 — runtime-learned API limit store
    "InMemoryRuntimeLimitStore",
    # sub-agent session persistence + participant (multi-agent groundwork)
    "SqliteSubAgentSessionRepository",
    "QueuedSubAgentSessionRepository",
    "SqliteParticipantRepository",
    # roster-template library (reusable discussion teams; pure V2 enhancement)
    "SqliteRosterTemplateRepository",
    # agent-template library (reusable single roles; pure V2 enhancement, §27)
    "SqliteAgentTemplateRepository",
    # mode-template library (collaboration modes; pure V2 enhancement, §26/§27)
    "SqliteModeTemplateRepository",
]
