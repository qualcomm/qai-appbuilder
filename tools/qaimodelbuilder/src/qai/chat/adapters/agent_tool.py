# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Agent tool handler — spawns sub-agent tasks (A6 feature).

The "agent" tool allows the LLM to delegate sub-tasks to a focused
sub-agent that runs its own agentic loop (up to ``max_rounds`` rounds).
The sub-agent does NOT have access to the "agent" tool itself, which
prevents infinite recursion.

Migrated from ``backend/chat_handler.py:run_subagent`` (lines 2188-2343).

Key differences from the legacy implementation:

* Sub-agent tool calls dispatch through a real
  :class:`~qai.chat.application.ports.ToolInvocationPort` when one is
  injected (see :meth:`_execute_tool_call`); when no executor is wired
  the handler returns a placeholder string so unit tests that only
  exercise the LLM streaming surface stay self-contained. The "agent"
  tool name itself is rejected at the dispatch site to prevent
  infinite recursion.
* Parallel dispatch of multiple ``agent`` tool_calls in a single LLM
  turn is provided by :meth:`execute_many`, which fans the requests
  through :func:`asyncio.gather` so the orchestrator sees ``max(t_i)``
  latency instead of ``sum(t_i)``.
* The handler depends only on ``LLMStreamPort`` and (optionally)
  ``ToolInvocationPort``; both ports are owned by the chat context, so
  the ``context-isolation`` import-linter contract is preserved.

PR-095 (S9 audit §3.1 F-7 sub-agent SSE parity, §2.2 H-21 parallel
sub-agents) extends this module:

* :meth:`AgentToolHandler.iter_events` — async iterator that yields
  structured SSE-shaped event dicts (``subagent_output``,
  ``subagent_tool``, ``subagent_done``).  The existing :meth:`execute`
  signature is preserved for backwards compatibility; it is now a
  thin wrapper that drains :meth:`iter_events` and concatenates the
  ``subagent_output`` fragments + the final ``subagent_done.text``.
* :meth:`AgentToolHandler.execute_many` — dispatches multiple
  sub-agent tasks via :func:`asyncio.gather` so a single LLM turn that
  emits N agent tool_calls completes in ``max(t_i)`` instead of
  ``sum(t_i)``.  Reference: legacy ``chat_handler.py:542-694``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import datetime
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from qai.chat.application.ports import (
    BudgetTrackerPort,
    ContextCompressionPort,
    GuardrailDecision,
    GuardrailPort,
    LLMStreamPort,
    LLMStreamRequest,
    PromptSnapshotStorePort,
    RetryPolicyPort,
    StreamAbortHandle,
    SubAgentSessionRepositoryPort,
    ToolInvocationPort,
    ToolInvocationRequest,
    ToolResultTruncatorPort,
    ToolStreamChunk,
    ToolStreamChunkKind,
)
from qai.chat.application.sub_agent_stream_broadcaster import (
    SubAgentStreamBroadcaster,
)
from qai.chat.adapters.sub_agent_abort_registry import (
    InMemorySubAgentAbortRegistry,
)
from qai.chat.adapters.budget_tracker import NullBudgetTracker
from qai.chat.application.use_cases._agentic_kernel import (
    AGED_TOOL_OUTPUT_PLACEHOLDER,
    COMPRESS_PRESERVE_TAIL as _COMPRESS_PRESERVE_TAIL,
    COMPRESS_TARGET_RATIO as _COMPRESS_TARGET_RATIO,
    DEFAULT_SUB_AGENT_MAX_ROUNDS as _DEFAULT_SUB_AGENT_MAX_ROUNDS,
    INTER_ROUND_COMPRESS_THRESHOLD_RATIO as _INTER_ROUND_COMPRESS_THRESHOLD_RATIO,
    age_old_tool_outputs,
    build_cancelled_tool_message as _build_cancelled_tool_message,
    tool_result_already_truncated as _kernel_tool_already_truncated,
    truncate_tool_result as _kernel_truncate_tool_result,
)
from qai.chat.application.use_cases._compaction_engine import (
    CompactionCheckpointEngine,
)
from qai.chat.application.provider_cache_capability import (
    ProviderCacheCapabilityRegistry,
)
from qai.chat.application.use_cases._tool_round_executor import (
    execute_tools_in_parallel_stream as _execute_tools_in_parallel_stream,
)
from qai.chat.application.use_cases._single_agent_turn import (
    KernelAborted,
    KernelChunk,
    KernelError,
    KernelFinished,
    KernelMaxRoundsReached,
    KernelRoundStarted,
    KernelStreamPassthrough,
    KernelToolCallsIssued,
    KernelToolPartial,
    KernelToolResult,
    RoundEndDecision,
    SingleAgentTurnKernel,
    ToolExecutionItem,
)
from qai.chat.application.use_cases._workspace_context import (
    resolve_workspace_context_files as _resolve_workspace_context_files,
)
from qai.chat.adapters.system_prompt_builder import (
    _DEFAULT_IDENTITY as _MAIN_AGENT_IDENTITY,
    RichSystemPromptBuilder as _RichSystemPromptBuilder,
    SUB_AGENT_SYSTEM_PROMPT as _SUB_AGENT_SYSTEM_PROMPT,
    _has_execution_tools as _has_execution_tools,
)
from qai.chat.application.use_cases.tool_advertise import (
    CONDITIONAL_TOOL_NAMES as _CONDITIONAL_TOOL_NAMES,
    SUB_AGENT_EXCLUDED_TOOLS as _SUB_AGENT_EXCLUDED_TOOLS,
    compose_advertised_tools as _compose_advertised_tools,
)
from qai.chat.domain.content import MessageContent, MessageRole
from qai.chat.domain.agent_profile import AgentProfile, GENERAL, resolve_profile
from qai.chat.domain.ids import (
    ConversationId,
    MessageId,
    SubAgentSessionId,
    TabId,
)
from qai.chat.domain.message import Message
from qai.chat.domain.model_profiles import get_context_limit, get_model_profile
from qai.chat.domain.stream_frame import StreamFrame, StreamFrameType
from qai.chat.application.use_cases._streaming_helpers import (
    classify_error_frame as _classify_error_frame,
    rebuild_history_wire_messages as _rebuild_history_wire_messages,
    repair_orphan_tool_messages as _repair_orphan_tool_messages,
    wire_to_structured_messages as _wire_to_structured_messages,
)
from qai.chat.application.use_cases._stream_guards import (
    abortable_frames as _abortable_frames,
    network_retrying_stream as _network_retrying_stream,
)
from qai.chat.application._token_estimate_helpers import (
    append_display_usage_fields as _append_display_usage_fields,
    effective_prompt_tokens as _effective_prompt_tokens,
    record_subagent_turn_usage as _record_subagent_turn_usage,
)
from qai.chat.domain.sub_agent_session import (
    SubAgentSession,
    SubAgentSessionStatus,
)
from qai.platform.ids import IdGenerator, UlidGenerator
from qai.platform.logging import get_logger
from qai.platform.scheduling.tool_concurrency import ToolConcurrencyManager
from qai.platform.time import Clock, SystemClock

__all__ = [
    "AgentToolHandler",
    "SubAgentEvent",
    # Backward-compat re-exports (historical module surface consumed by tests):
    # the shared main-agent identity + composed sub-agent behavioural-rules
    # block now LIVE in ``system_prompt_builder`` (single source of truth), but
    # remain importable from here so existing importers/tests are unaffected.
    "_MAIN_AGENT_IDENTITY",
    "_SUB_AGENT_SYSTEM_PROMPT",
]

_log = get_logger(__name__)

# Sub-agent system prompt (behavioural rules only — no identity line here).
# ``_SUB_AGENT_SYSTEM_PROMPT`` is now IMPORTED from ``system_prompt_builder``
# (the single source of truth for prompt assembly): that module composes the
# shared sub-agent behavioural-rules sections (all text lives in
# ``prompts/default_agent.txt`` so it can be edited as prose without touching
# Python) and OWNS the concise-sub-agent assembly via
# ``RichSystemPromptBuilder.build_sub_agent_concise`` (called from
# :meth:`AgentToolHandler._build_system_text`). This module no longer hand-rolls
# the f-string pipeline — there is ONE place system prompts are built.
#
# The "general" sub-agent design: the main-agent identity
# (_DEFAULT_IDENTITY / "You are QAI ModelBuilder...") is prepended by the builder
# for the general path, so the sub-agent knows who it is without a separate "you
# are a sub-agent" role line. Profile-specific sub-agents (e.g. explore) supply
# their own role via profile.system_prompt.
#
# Sections composed into ``_SUB_AGENT_SYSTEM_PROMPT`` (behavioural rules,
# after identity): subagent_principles / language_rule / subagent_no_raw_output
# / subagent_filesystem_safety / subagent_final_reply_format /
# subagent_exploration_thrift. Appended by the builder's concise assembly:
# parallel_tools (always) / python_env (exec-gated) / mermaid (always) /
# Working Directory + inherited AGENTS.md·CLAUDE.md context.

# Tools the sub-agent must NEVER receive. The sub-agent is an ORDINARY chat
# turn with a restricted tool set — per the user's design the ONLY differences
# from the main agent are:
#   * ``agent`` — excluded so a sub-agent cannot spawn further sub-agents
#     (recursion guard; V1 parity: legacy ``run_subagent`` calls
#     ``_stream_cloud(extra_tools=None)`` so the agent tool is never advertised
#     to the child — ``chat_handler.py:2246-2249``).
#   * ``question`` — excluded because a sub-agent's blocking ``question`` would
#     wait for a user answer whose dialog is NOT reliably reachable unless the
#     user has the sub-agent's tab open (it would wedge the run). The sub-agent
#     should complete autonomously and hand its result back to the main agent.
#   * ``list_subagents`` — excluded because a sub-agent cannot spawn (or resume)
#     sub-agents at all (the ``agent`` tool is removed above), so enumerating the
#     parent conversation's sub-agents has no actionable use and would only leak
#     sibling/parent context into a child run.
# EVERYTHING ELSE is shared with the main agent — including the conditional
# app-builder tools (``appbuilder_run``), which the sub-agent now receives on
# the SAME terms as the main agent (cloud + app-builder mode), via
# ``_sub_agent_tool_schemas`` below. (Previously ``appbuilder_run`` was wrongly
# excluded here, diverging from the main agent.)
# ``_SUB_AGENT_EXCLUDED_TOOLS`` (agent / question / list_subagents) and
# ``_CONDITIONAL_TOOL_NAMES`` (appbuilder_run / appbuilder_batch_run) now live
# in the SHARED application-layer helper ``tool_advertise`` (single source of
# truth — previously duplicated here and in ``streaming``). Imported above with
# their historical private aliases so the rest of this module is unchanged.

# Per-tool-result truncation is now delegated to the SHARED, model-aware
# ``ToolResultTruncatorPort`` (the SAME instance the parent agentic loop uses)
# via ``_agentic_kernel.truncate_tool_result`` — see :meth:`_iter_loop`. The
# sub-agent previously head-truncated to a hard-coded 2 000 chars; unifying on
# the parent's truncator makes the cap model-aware (Claude/Gemini 100k → 10k
# under pressure) AND tunable in ONE place for both loops (user requirement).
# A best-effort fallback char cap is used only when no truncator is wired
# (legacy unit stubs) so those callers keep working (V1/v0.5 parity — small
# unbounded outputs).
_SUB_AGENT_FALLBACK_TOOL_OUTPUT_MAX_CHARS = 50_000

# Number of most-recent messages the compressor preserves intact is the shared
# kernel constant ``_COMPRESS_PRESERVE_TAIL`` (imported above).

# Placeholder prompt for the sub-agent's ``LLMStreamRequest``: the sub-agent
# always hands its fully-assembled wire history down via ``extra["messages"]``
# (which both adapters honour in preference to ``prompt`` / ``history`` —
# ``llm_stream.py:1145-1147`` / ``local_model_stream.py:813-843``), so the
# ``prompt`` field is never read. ``MessageContent`` rejects empty text, so a
# minimal non-empty sentinel is used; it never reaches the wire.
_EMPTY_PROMPT = MessageContent(text="(sub-agent uses extra['messages'])")

# Local (on-device) model marker — kept in sync with
# ``streaming._LOCAL_MODEL_HINT_PREFIX`` / V1 ``chat_handler.py:400``. The
# workspace project-context (AGENTS.md / CLAUDE.md) injection is CLOUD-ONLY,
# so the sub-agent skips it when its inherited ``model_hint`` is local.
_LOCAL_MODEL_HINT_PREFIX = "local::"


def _is_local_model_hint(model_hint: str | None) -> bool:
    return isinstance(model_hint, str) and model_hint.startswith(
        _LOCAL_MODEL_HINT_PREFIX
    )


def _is_anthropic_family(model_id: str | None) -> bool:
    """Return True when ``model_id`` is a Claude / Anthropic-family model.

    Local mirror of the main chat's
    ``qai.chat.application.use_cases.streaming._is_anthropic_family`` (kept
    LOCAL so the ``adapters`` layer does not import from ``application`` —
    no new cross-layer edge, no import cycle). Used to decide whether a
    round's ``cache_read_tokens`` must be added back into the effective wire
    size: Anthropic/Claude split cache reads OUT of ``prompt_tokens`` (so the
    real wire is ``prompt_tokens + cache_read_tokens``), whereas OpenAI /
    Azure / Gemini / Vertex already fold cache into ``prompt_tokens``. We key
    on the MODEL ID (``"claude" in model_id``) — the authoritative selector —
    not the client-supplied provider field.
    """
    return isinstance(model_id, str) and "claude" in model_id.lower()


def _sub_agent_tool_schemas(
    tool_executor: ToolInvocationPort | None,
    *,
    tool_mode: str | None = None,
    is_local: bool = False,
    profile: AgentProfile | None = None,
    allow_spawn: bool = False,
    disabled_tools: frozenset[str] = frozenset(),
    enable_skill: bool = True,
) -> list[dict[str, Any]]:
    """Return the tool schemas advertised to a sub-agent's LLM turns.

    The sub-agent runs the FULL builtin tool set so it can actually *do work*
    (read / write / edit / exec / webfetch / glob / grep / apply_patch …)
    across its agentic loop, NOT merely answer in one text turn. Per the
    user's design the sub-agent set is IDENTICAL to the main agent's except:

    * ``agent`` is removed by default (recursion guard) — UNLESS
      ``allow_spawn=True`` (V2 enhancement; the per-tab "allow first-level
      sub-agents to spawn their own sub-agents" / "allow this sub-agent to
      spawn" switch is on). When allowed, the ``agent`` schema is kept so the
      sub-agent's LLM may request nested spawning; ``_execute_tool_call`` then
      runs the grand sub-agent via :meth:`AgentToolHandler.iter_events` with
      ``allow_spawn=False`` (recursion 封顶: a sub-agent permitted to spawn
      produces children that, by default, are NOT permitted to spawn further,
      so the tree stops at the next level unless the user opts that level in
      too).
    * ``question`` is removed (its blocking dialog is not reliably reachable
      from a background sub-agent).

    The conditional app-builder tools (``appbuilder_run`` /
    ``appbuilder_batch_run``) are handled EXACTLY like the main agent
    (``streaming._collect_tools_schemas``): dropped from the base set and
    re-added ONLY for cloud turns in ``app-builder`` mode — so a sub-agent
    spawned in an app-builder cloud session gets ``appbuilder_run`` too.

    ``profile`` (V2 enhancement) applies a per-profile allow/deny policy ON TOP
    OF the base filtering above: ``GENERAL`` (or ``None``) is a no-op (the set
    is byte-for-byte what it was before profiles existed); ``EXPLORE`` strips
    every state-mutating tool, leaving only the read-only search surface
    (read/glob/grep/webfetch/list). The filter runs LAST so it also
    drops any conditional app-builder tool that the base logic re-added.

    ``enable_skill`` (方案Y — autonomous sub-agent skill gate): governs whether
    the ``skill`` tool may be advertised. It DEFAULTS to ``True`` at this helper
    level ONLY to keep the pre-refactor byte-for-byte equivalence contract
    (``test_tool_advertise_unify`` compares this helper's default-arg output to
    the historical inline formula); the SOLE production caller (the autonomous
    dispatch in :meth:`AgentToolHandler.iter_events`) always passes
    ``enable_skill=False`` — so an autonomously-dispatched sub-agent NEVER
    receives ``skill`` (product decision: sub-agents get no skill, no front-end
    entry, no ``enabled_skills`` propagation). When ``False`` the ``skill``
    schema is dropped in the composer's Step 1 — this takes effect EVEN when a
    profile allow-list (e.g. ``EXPLORE``) still names ``skill``, because the
    drop happens BEFORE the profile filter. The main agent / take-over paths do
    NOT go through this function (they compose via
    ``streaming._collect_tool_schemas`` with ``enable_skill`` defaulting to
    ``True``), so they keep the ``skill`` tool and are unaffected.

    Returns ``[]`` when the executor exposes no ``schemas()`` (e.g. a test
    stub), in which case the sub-agent degrades to text-only.
    """
    if tool_executor is None:
        return []
    schemas_fn = getattr(tool_executor, "schemas", None)
    if not callable(schemas_fn):
        return []
    try:
        advertised = schemas_fn()
    except Exception:  # noqa: BLE001 — best-effort; never block the sub-agent
        return []

    # When ``allow_spawn`` is set, the sub-agent IS permitted to spawn its own
    # sub-agents this run, so BOTH spawn-related tools are un-excluded together
    # (``agent`` to spawn + ``list_subagents`` to look up a child's id). The
    # main-agent / take-over loop derives its ``excluded`` set the SAME way (see
    # ``streaming._collect_tool_schemas``), so the two paths share one口径.
    # ``question`` stays excluded either way (its blocking dialog is not
    # reliably reachable from a background sub-agent).
    excluded = (
        _SUB_AGENT_EXCLUDED_TOOLS - {"agent", "list_subagents"}
        if allow_spawn
        else _SUB_AGENT_EXCLUDED_TOOLS
    )

    # The ``agent`` schema is the SINGLE source of truth in the use case
    # (reuse > duplicate); imported lazily here (adapters→application is the
    # allowed layer direction and a deferred import avoids any module-load
    # cycle) and handed to the shared composer as the factory.
    def _agent_schema_factory() -> dict[str, Any]:
        from qai.chat.application.use_cases.streaming import (
            _agent_tool_schema,
        )

        return _agent_tool_schema()

    # Whole filter/assemble flow is the SHARED helper (base exclude + conditional
    # drop → inject agent (if spawning) → cloud+app-builder re-add → profile
    # filter LAST → per-session disabled). Byte-for-byte equivalent to the
    # pre-refactor inline pipeline this function carried.
    return _compose_advertised_tools(
        advertised,
        tool_mode=tool_mode,
        is_local=is_local,
        excluded=excluded,
        inject_agent=allow_spawn,
        agent_schema_factory=_agent_schema_factory,
        profile=profile,
        disabled_tools=disabled_tools,
        enable_skill=enable_skill,
    )



# ---------------------------------------------------------------------------
# Wire-history block builders (shared by the sub-agent loop)
# ---------------------------------------------------------------------------
# These mirror the parent agentic loop's ``streaming.py:_append_tool_round``
# (2541-2630) and V1 ``chat_handler.py:2276-2329`` so the sub-agent's
# ``extra["messages"]`` carries the exact OpenAI wire shape the parent uses:
# ``assistant{content, tool_calls:[{id,type,function}]}`` immediately followed
# by one ``tool{tool_call_id}`` per call, with strict id pairing.
def _build_tool_metas(
    tool_calls: list[dict[str, Any]],
    round_num: int,
) -> list[tuple[str, dict[str, Any], str]]:
    """Normalise this round's TOOL_CALL frame payloads into ``(name, args, id)``.

    The id is the one the model emitted (frame payload ``tool_call_id``) when
    present, else a round-unique synthetic id (never a bare ``call_{i}`` which
    would collide across rounds when the upstream omits ids — same concern as
    the parent loop's ``streaming.py:2580-2583``).  Non-dict ``arguments`` are
    coerced to ``{}`` so the downstream ``json.dumps`` never raises.
    """
    metas: list[tuple[str, dict[str, Any], str]] = []
    for idx, tc in enumerate(tool_calls):
        tool_name = tc.get("tool_name", "unknown")
        arguments = tc.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}
        call_id = tc.get("tool_call_id") or f"sub_{round_num}_{idx}"
        metas.append((tool_name, arguments, call_id))
    return metas


# The OpenAI wire-block builders (``assistant.tool_calls`` array + paired
# ``role:tool`` replies) and the per-result truncation now live in the shared
# neutral kernel (``_agentic_kernel.build_assistant_tool_calls_block`` /
# ``build_tool_reply_blocks`` / ``truncate_tool_result``) so the sub-agent loop
# and the parent agentic loop render the EXACT same wire shape and truncate to
# the SAME model-aware budget — see :meth:`AgentToolHandler._iter_loop`.


# ---------------------------------------------------------------------------
# Completed-round record for the differential compaction engine
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _SubAgentCompletedRound:
    """One sub-agent tool round's wire block + provider-measured prompt size.

    A LOCAL, duck-typed mirror of ``streaming._CompletedRound`` (the main
    agent's record). The shared :class:`CompactionCheckpointEngine` consumes
    ``completed_rounds`` ONLY via duck-typing (``.real_prompt_tokens`` /
    ``.source`` / ``.text`` / ``.tool_calls`` / ``.tool_results``) — see
    ``_compaction_engine`` module docstring — so this loop owns its own record
    type WITHOUT importing ``streaming.py`` (which imports the engine; a
    cross-import would create a cycle AND drag the ``Conversation`` aggregate
    into the ``adapters`` layer). Field names + meanings are byte-for-byte the
    main agent's so the engine attributes real tokens identically for both.

    State-Truth-First (AGENTS.md 铁律 1): ``real_prompt_tokens`` is filled from
    the provider's per-round measurement (or a bounded tokenizer fallback on
    just THIS round's incremental content), NEVER a char-density estimate.
    """

    text: str
    tool_calls: list  # type: ignore[type-arg]
    tool_results: list[dict[str, Any]]
    real_prompt_tokens: int = 0
    completion_tokens: int = 0
    source: str = "unknown"


# ---------------------------------------------------------------------------
# Merged cooperative-abort handle (runtime-defect fix — two-registry bridge)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _MergedAbortHandle:
    """Cooperative-abort view that ORs a sub-agent event + a parent check.

    Root-cause fix for "主 Agent 停止后子 Agent 一直跑不停": a spawned
    sub-agent used to observe ONLY its own ``subagent_abort_registry`` event,
    which the main-agent tab Stop never set (it only signals
    ``stream_abort_registry`` keyed by ``tab_id``). This handle lets the
    sub-agent's round loop + per-round LLM stream guards observe BOTH:

    * ``event`` — this sub-agent's own registry :class:`asyncio.Event`
      (standalone tab ⏹, the manual ``/subagents/{id}/interrupt`` endpoint, or
      the Fix-1 owner-tab cascade all set it);
    * ``parent_abort_check`` — a zero-arg predicate reading the PARENT tab's
      stream-abort handle ``is_set`` (the main agent tab's ⏹), threaded down
      from the spawning ``StreamChatUseCase`` turn.

    Exposes the minimal :class:`~qai.chat.application.ports.StreamAbortHandle`
    surface (``is_set()``) used by the shared stream guards. State-Truth-First
    (AGENTS.md 铁律 1): :meth:`is_set` is True ONLY when a real source is set —
    a missing/None source never fabricates an abort, so a running turn whose
    parent has NOT stopped is never falsely aborted.
    """

    event: "asyncio.Event | None" = None
    parent_abort_check: Callable[[], bool] | None = None

    @property
    def active(self) -> bool:
        """Whether ANY abort source is wired (else the guards degrade to None)."""
        return self.event is not None or self.parent_abort_check is not None

    def is_set(self) -> bool:
        if self.event is not None and self.event.is_set():
            return True
        if self.parent_abort_check is not None:
            try:
                return bool(self.parent_abort_check())
            except Exception:  # noqa: BLE001 — a predicate error must NOT abort
                return False
        return False


def _stamp_wire_turn_usage(
    wire_messages: list[dict[str, Any]],
    seed_len: int,
    round_usages: dict[int, dict[str, Any]],
    round_request_ids: dict[int, str] | None = None,
) -> None:
    """Stamp per-round ``request_id`` + token ``usage`` onto assistant wire turns.

    Extracted from the autonomous-completion path so it is SHARED by BOTH
    :meth:`AgentToolHandler._finalize_session` (via the ``_iter_loop`` tail) AND
    the mid-run / interrupt persistence paths (``_record_round``). Without this
    sharing a run that is ``aclose``'d mid-flight (e.g. a nested / grand
    sub-agent whose parent turn was cancelled) skipped the tail stamp and
    persisted a transcript with NO per-message ``usage`` — the front-end's
    ``cumulativeInputNew`` then found no usage-bearing assistant message and
    hid the ↑↓ token line (the reported grandchild bug). Stamping here too
    means EVERY persisted snapshot (any depth) carries the same per-message
    token line the main agent shows.

    回看 parity with the main agent: each kernel round appends exactly ONE
    assistant turn to ``wire_messages`` (tool round → assistant{tool_calls}
    block; final round → the assistant text turn), in round order. Walk THIS
    run's newly-appended assistant turns (``wire_messages[seed_len:]``) and map
    the i-th NEW assistant turn to kernel round ``i + 1`` (``round_no`` is
    1-based).

    DISPLAY-ONLY: the ``usage`` dict is tail-appended with the shared
    ``_append_display_usage_fields`` (same as the main agent) — the counter /
    eff-prompt / billing math never read these keys. ``dict(_u)`` so the
    per-round snapshot in ``round_usages`` is not mutated.

    IDEMPOTENT: a turn that ALREADY carries a ``usage`` dict is left untouched
    (so a later ``_record_round`` / finalize re-stamp on the SAME wire does not
    double-append display fields or clobber an earlier stamp); ditto for a turn
    that already carries a ``request_id``.
    """
    if not (round_request_ids or round_usages):
        return
    _round_seq = 0
    for _turn in wire_messages[seed_len:]:
        if not isinstance(_turn, dict) or _turn.get("role") != "assistant":
            continue
        _round_seq += 1
        if round_request_ids and not _turn.get("request_id"):
            _rid = round_request_ids.get(_round_seq)
            if _rid:
                _turn["request_id"] = _rid
        # Idempotent: skip a turn that was already stamped with usage (avoids
        # re-appending the DISPLAY-ONLY fields when the SAME wire is persisted
        # more than once — mid-run ``_record_round`` then a terminal finalize).
        if _turn.get("usage"):
            continue
        _u = round_usages.get(_round_seq)
        if _u:
            _turn["usage"] = _append_display_usage_fields(dict(_u), _u)


# ---------------------------------------------------------------------------
# Event shape (PR-095 / S9 F-7)
# ---------------------------------------------------------------------------
# ``SubAgentEvent`` is a plain dict — keeping the type loose here lets
# the SSE encoder serialise the payload without an extra adapter.  The
# ``type`` discriminator names mirror the legacy frontend listeners
# (``subagent_output`` / ``subagent_tool`` / ``subagent_done``).
SubAgentEvent = dict[str, Any]


class AgentToolHandler:
    """Handles the 'agent' tool — spawns sub-agent tasks.

    The handler runs a simplified agentic loop: it streams completions
    from the LLM port, accumulates text across rounds, and handles
    tool_call frames by dispatching them to a real tool executor
    (excluding the "agent" tool itself to prevent infinite recursion).

    Usage::

        handler = AgentToolHandler(llm=llm_port, tool_executor=tools)
        tools.register("agent", handler.execute)
    """

    __slots__ = (
        "_abort_registry",
        "_budget_tracker",
        "_clock",
        "_compaction_engine",
        "_compress_threshold_ratio",
        "_compressor",
        "_fallback_tool_output_max_chars",
        "_frame_stall_budget_s",
        "_guardrail_factory",
        "_ids",
        "_kernel",
        "_llm",
        "_max_rounds",
        "_max_spawn_depth",
        "_prompt_snapshot_store",
        "_provider_cache_registry",
        "_profile_model_overrides_provider",
        "_retry_policy",
        "_sleep",
        "_stream_broadcaster",
        "_sub_agent_sessions",
        "_tool_concurrency",
        "_tool_executor",
        "_tool_result_truncator",
    )

    def __init__(
        self,
        *,
        llm: LLMStreamPort,
        tool_executor: ToolInvocationPort | None = None,
        max_rounds: int = _DEFAULT_SUB_AGENT_MAX_ROUNDS,
        compressor: ContextCompressionPort | None = None,
        tool_result_truncator: ToolResultTruncatorPort | None = None,
        sub_agent_sessions: SubAgentSessionRepositoryPort | None = None,
        stream_broadcaster: "SubAgentStreamBroadcaster | None" = None,
        abort_registry: "InMemorySubAgentAbortRegistry | None" = None,
        prompt_snapshot_store: PromptSnapshotStorePort | None = None,
        ids: IdGenerator | None = None,
        clock: Clock | None = None,
        compress_threshold_ratio: float = (
            _INTER_ROUND_COMPRESS_THRESHOLD_RATIO
        ),
        compaction_ratio_provider: (
            Callable[[], Awaitable[dict[str, float]] | dict[str, float]] | None
        ) = None,
        tool_concurrency: ToolConcurrencyManager | None = None,
        retry_policy: RetryPolicyPort | None = None,
        frame_stall_budget_s: float = 600.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        guardrail_factory: Callable[[], GuardrailPort] | None = None,
        max_spawn_depth: int = 8,
        provider_cache_registry: "ProviderCacheCapabilityRegistry | None" = None,
        profile_model_overrides_provider: (
            Callable[[], Awaitable[dict[str, str]] | dict[str, str]] | None
        ) = None,
        # ---- Per-conversation token-budget tracker (max_budget_tokens) ----
        # Optional ``BudgetTrackerPort`` — the SAME instance the main agent
        # holds (shared via DI) so a sub-agent draws from the PARENT
        # conversation's budget pool (user's mental model: "this whole chat has
        # an N-token budget"). Each sub-agent round folds its provider-measured
        # net-new tokens into the pool keyed by the PARENT ``conversation_id``
        # and, before opening another round, aborts with the SAME
        # ``budget_exceeded`` semantics once the cap is met. ``None`` (default,
        # legacy / unit stubs) → a no-op ``NullBudgetTracker`` so behaviour is
        # byte-for-byte unchanged. Local turns / no-usage rounds are never
        # counted or blocked (State-Truth-First).
        budget_tracker: "BudgetTrackerPort | None" = None,
    ) -> None:
        self._llm = llm
        self._tool_executor = tool_executor
        self._max_rounds = max_rounds
        # Unified spawn-path recursion ceiling (alpha step). ``iter_events``
        # tracks a ``spawn_depth`` (1 = a first-level sub-agent spawned by the
        # main agent, 2 = grand, 3 = great-grand, …) and refuses to spawn once
        # that depth reaches ``max_spawn_depth`` — a diagnostic ``[error: max
        # sub-agent nesting depth (N) reached]`` string is returned to the
        # spawning LLM instead of another recursion. This replaces the old
        # hard ``allow_spawn=False`` guard in ``_spawn_grand_sub_agent`` (which
        # capped recursion at exactly 2 levels with no per-level knob); the
        # per-level user opt-in (``allow_spawn`` on each session) is orthogonal
        # and still governs whether a specific run may create direct children.
        # DI wires this from ``Settings.chat_max_spawn_depth`` (default 8);
        # a clamp to >= 1 keeps a degenerate 0/negative value from silently
        # blocking every first-level spawn.
        self._max_spawn_depth = max(1, int(max_spawn_depth))
        # 方案B: shared gateway prompt-cache-support registry (SAME instance as
        # the main agent + routing stream, via DI). The per-round aging gate in
        # ``_open_round_stream`` consults ``aging_enabled`` before calling
        # ``age_old_tool_outputs``; ``_on_round_end`` writes the gateway's cache
        # support back via ``mark`` from the round usage's
        # ``provider_reported_cache`` flag. ``None`` (legacy / unit stubs) → the
        # gate falls back to unconditional aging (prior behaviour, byte-for-byte).
        self._provider_cache_registry = provider_cache_registry
        # Per-profile model override provider (V2 enhancement — per-profile
        # model). A zero-arg callable (sync OR async) the DI/composition root
        # wires to read the user's per-profile model configuration, returning a
        # ``{profile_name: model_id}`` mapping (e.g.
        # ``{"explore": "aicegrok/…", "general": ""}``). ``_iter_loop`` reads it
        # (best-effort, per run so a settings change takes effect next run) and,
        # for the resolved profile, passes any NON-BLANK entry as
        # ``resolve_profile(..., model_override=...)`` so that profile's
        # sub-agent routes to the chosen model; a blank / missing entry (or a
        # ``None`` provider, or a provider that raises) ⇒ inherit the parent's
        # ``model_hint`` EXACTLY as before (identity-preserving no-op). The
        # domain hard-codes no model — the id always originates here.
        self._profile_model_overrides_provider = (
            profile_model_overrides_provider
        )
        # P4 — shared LLM-stream guards (single source of truth with the main
        # agent's ``StreamChatUseCase._abortable_frames`` /
        # ``_network_retrying_stream``). When wired, the per-round LLM stream
        # opened in ``_open_round_stream`` is wrapped with the SAME helpers the
        # main agent uses:
        #   * ``_network_retrying_stream`` — indefinite NETWORK auto-retry
        #     (transient connect / timeout / read / socket failures) with
        #     escalating backoff and a transient ``network_retry`` progress
        #     frame for UI keep-alive; pure pass-through when
        #     ``retry_policy=None`` (legacy / unit-stub parity).
        #   * ``_abortable_frames`` — races each upstream ``__anext__`` against
        #     a short poll window so a user Stop interrupts a half-open /
        #     wedged cloud SSE in 50ms instead of waiting for the 600s httpx
        #     read-timeout, AND bounds the gap between meaningful frames by
        #     ``frame_stall_budget_s`` so an upstream that holds the socket
        #     open with keep-alive bytes but never advances application state
        #     does not pin the sub-agent "running" forever (the very failure
        #     mode the main agent already guards against).
        # ``sleep`` is the awaitable sleeper threaded into the abortable
        # backoff; defaults to ``asyncio.sleep`` so unit stubs without a
        # clock-injected sleeper work unchanged.
        self._retry_policy = retry_policy
        self._frame_stall_budget_s = max(0.0, float(frame_stall_budget_s))
        self._sleep: Callable[[float], Awaitable[None]] = (
            sleep if sleep is not None else asyncio.sleep
        )
        # P9 — per-run :class:`GuardrailPort`. The main agent
        # (``StreamChatUseCase``) injects a ``guardrail_factory`` so each
        # turn gets its own fresh guardrail instance (the controller
        # accumulates per-turn observations that must not leak across
        # turns); the sub-agent now does the same. When unwired
        # (legacy / unit stubs), :meth:`_execute_tool_call` skips the
        # guardrail check/observe entirely and behaves exactly as
        # before — profile deny-list + per-session disabled-tools
        # remain authoritative (defence-in-depth was always 3-layered:
        # advertise filter / handler gate / now guardrail too).
        self._guardrail_factory: Callable[[], GuardrailPort] | None = (
            guardrail_factory
        )
        # Multi-round context management (V2 enhancement over V1/v0.5, which
        # ran the sub-agent loop with NO compression and NO per-tool-output
        # cap — fine for small tasks but unbounded for long multi-round runs
        # with large tool outputs, eventually overflowing the cloud context
        # window → ``prompt_too_long``. V2 reuses the SAME context-compression
        # abstraction the parent agentic loop uses, applied to the sub-agent's
        # own wire history. Both are OPTIONAL: when unwired the loop behaves
        # exactly as before (V1/v0.5 parity — no regression for light callers
        # / unit stubs).
        self._compressor = compressor
        # Compress when the running wire history is estimated to exceed this
        # fraction of the model's context window (leaves headroom for the
        # next completion). Defaults to the SHARED kernel threshold (0.80) so
        # the sub-agent and the parent loop compress at the SAME pressure
        # point (user requirement: limits统一可调). Clamped to (0.1, 1.0].
        self._compress_threshold_ratio = min(
            max(compress_threshold_ratio, 0.1), 1.0
        )
        # Per-tool-result truncation: the SHARED, model-aware
        # ``ToolResultTruncatorPort`` (the SAME instance the parent agentic
        # loop uses) so both loops truncate a single oversized output to the
        # SAME budget, tunable in ONE place. When unwired (legacy unit stubs),
        # a coarse char-cap fallback keeps those callers working.
        self._tool_result_truncator = tool_result_truncator
        self._fallback_tool_output_max_chars = (
            _SUB_AGENT_FALLBACK_TOOL_OUTPUT_MAX_CHARS
        )
        # Sub-agent session persistence (V2 enhancement; V1 discarded the
        # sub-agent's wire history entirely). When a repo is wired, every
        # sub-agent run is persisted as a :class:`SubAgentSession` so the
        # main agent can WAKE it up (``resume_session_id``) with its prior
        # context for a related follow-up task, and the user can open / take
        # it over in a new tab. ``ids`` / ``clock`` default to process
        # singletons so existing callers (and unit stubs) that do not inject
        # them keep working; persistence is simply skipped when no repo is
        # wired (full V1/legacy-stub parity — no regression).
        self._sub_agent_sessions = sub_agent_sessions
        self._ids: IdGenerator = ids if ids is not None else UlidGenerator()
        self._clock: Clock = clock if clock is not None else SystemClock()
        # Independent live-event fan-out (block 2). When wired, every event
        # this handler yields is ALSO published to the broadcaster keyed by
        # the sub-agent's persisted id, so a standalone sub-agent tab can
        # subscribe to ``GET /api/chat/subagents/{id}/stream`` and see live
        # progress (the parent-tab inline forwarding is unchanged — this is a
        # SECOND, independent channel, not a replacement). No-op when unwired
        # (legacy/stub parity) or when the session is not persisted (no id).
        self._stream_broadcaster = stream_broadcaster
        # Block 3 — independent per-sub-agent cancellation. When wired, the
        # loop registers a cancellation flag keyed by the sub-agent id, polls
        # it between rounds (cooperative abort — finishes the in-flight round
        # then stops), and unregisters on exit. The interrupt endpoint signals
        # this flag so a standalone sub-agent tab's "stop" actually stops ONLY
        # that sub-agent (not the whole parent tab). No-op when unwired.
        self._abort_registry = abort_registry
        # Per-round prompt-snapshot store (回看 parity with the main agent).
        # When wired, every sub-agent round saves the EXACT wire it sent (system
        # + accumulating history + per-round assistant{tool_calls}/tool blocks +
        # resolved tools/sampling) into the SAME ``PromptSnapshotStorePort`` the
        # main agent uses, mints a per-round ``request_id``, and stamps that id
        # (plus the round's token usage) onto the persisted assistant turn — so
        # a standalone sub-agent tab's per-message 📄 button opens the IDENTICAL
        # snapshot dialog the main agent shows (one shared store + one shared
        # render path; no sub-agent-specific snapshot UI). No-op when unwired
        # (legacy/stub parity — no snapshots, no per-message request_id/usage).
        self._prompt_snapshot_store = prompt_snapshot_store
        # Shared cross-agent tool concurrency budget (parallel-tool §5): the
        # SAME instance the main agent draws from, so N sub-agents x M tools
        # never storms the machine with subprocesses. None for legacy/stub
        # callers → unbounded (zero regression).
        self._tool_concurrency = tool_concurrency
        # Shared round-iteration skeleton (block §15): the SAME kernel the
        # main agent loop drives. The sub-agent's per-round skeleton (abort
        # check → maybe_compress_wire → build send wire → drain & classify →
        # build assistant{tool_calls} + execute + paired role:tool replies →
        # grow wire → cap at max_rounds) is the kernel's; this handler keeps
        # ONLY the sub-agent-specific shell (SubAgentSession persistence + CAS,
        # broadcaster, wake/take-over, independent abort, start/done/INCOMPLETE
        # markers). The kernel reuses ``_agentic_kernel``'s blocks, so the wire
        # the sub-agent grows is byte-for-byte what it grew before. The
        # truncator/compressor are the SAME instances threaded above.
        self._kernel = SingleAgentTurnKernel(
            compressor=compressor,
            truncator=tool_result_truncator,
            compress_threshold_ratio=self._compress_threshold_ratio,
        )
        # Differential-checkpoint compaction engine (V2 enhancement —
        # BYTE-FOR-BYTE the SAME algorithm the MAIN agent uses). The sub-agent
        # NEWs its own engine instance (依赖更少: no extra DI plumbing of an
        # engine handle) sharing the SAME ``compressor`` the main agent shares,
        # and the SAME user-chosen ratios (via ``compaction_ratio_provider`` —
        # the forge.config Agent-Loop sliders). The checkpoint cache is a PURE
        # in-memory dict (``checkpoint_store=None``): a sub-agent run is
        # ephemeral, so its compaction checkpoint need not survive a restart
        # (unlike a persisted ``Conversation``). ``key_prefix="subagent:"``
        # namespaces the cache so a sub-agent whose id collides with a
        # conversation id (it never does — different ULID spaces — but defence
        # in depth) can never read/write the main agent's checkpoint.
        #
        # When no ``compressor`` is wired (legacy unit stubs), the engine's
        # ``maybe_compress`` short-circuits to ``None`` (no-op), so the
        # sub-agent's ``_compact_hook`` falls through to the kernel's built-in
        # ``maybe_compress_wire`` — full V1/stub parity, no regression. This is
        # a NET ENHANCEMENT over the kernel's plain wire compression: the engine
        # adds the differential real-token attribution + a checkpoint so a
        # post-compaction round reuses the compacted head instead of
        # re-compressing the full wire each pressure point.
        self._compaction_engine = CompactionCheckpointEngine(
            compressor=compressor,
            ratio_provider=compaction_ratio_provider,
            checkpoint_store=None,
            threshold_ratio=self._compress_threshold_ratio,
            target_ratio=_COMPRESS_TARGET_RATIO,
            preserve_tail=_COMPRESS_PRESERVE_TAIL,
            key_prefix="subagent:",
        )
        # Per-conversation token-budget tracker (max_budget_tokens); ``None`` →
        # NullBudgetTracker (disabled, byte-for-byte unchanged). Sub-agents
        # share the parent conversation's pool (keyed by the parent conv id).
        if budget_tracker is not None:
            self._budget_tracker: BudgetTrackerPort = budget_tracker
        else:
            self._budget_tracker = NullBudgetTracker()

    async def execute(self, request: ToolInvocationRequest) -> str:
        """Execute a sub-agent task.

        Extracts ``description`` from ``request.arguments``, then runs
        a multi-round agentic loop to completion.

        Returns the sub-agent's final output text (accumulated over all
        rounds).  If the loop exhausts ``max_rounds`` without a clean
        text-only finish, returns a truncation notice.

        Backward-compatible wrapper: PR-095 introduced
        :meth:`iter_events` as the new richer surface; this method
        drains that iterator and concatenates the text fragments so
        existing callers see exactly the same string they used to.
        """
        text_parts: list[str] = []
        final_text: str | None = None
        async for event in self.iter_events(request):
            etype = event.get("type")
            if etype == "subagent_output":
                text_parts.append(str(event.get("content", "")))
            elif etype == "subagent_done":
                final_text = str(event.get("result", "")) or None
            elif etype == "subagent_error":
                # Surface error verbatim — keeps legacy semantics.
                return f"[sub-agent error: {event.get('message', 'unknown')}]"
        if final_text is not None:
            return final_text
        joined = "".join(text_parts).strip()
        return joined or "[sub-agent produced no output]"

    async def execute_many(
        self,
        requests: Sequence[ToolInvocationRequest],
    ) -> list[str]:
        """Run *requests* in parallel via :func:`asyncio.gather`.

        PR-095 / S9 H-21 parity with legacy
        ``chat_handler.py:542-694``: when the LLM emits multiple
        ``agent`` tool_calls in a single round, the orchestrator
        should dispatch them concurrently so wall-clock latency is
        ``max(t_i)`` not ``sum(t_i)``.

        Each entry in the returned list pairs 1:1 with ``requests``
        positionally; failures are caught per-task and surfaced as a
        ``[sub-agent error: ...]`` string so a single failure does
        not abort the whole batch.
        """
        if not requests:
            return []
        coroutines = [self.execute(req) for req in requests]
        results = await asyncio.gather(*coroutines, return_exceptions=True)
        out: list[str] = []
        for item in results:
            if isinstance(item, BaseException):
                out.append(f"[sub-agent error: {item}]")
            else:
                out.append(str(item))
        return out

    async def iter_events(
        self,
        request: ToolInvocationRequest,
        *,
        agent_index: int = 0,
        total_agents: int = 1,
        model_hint: str | None = None,
        tool_mode: str | None = None,
        workspace_root: str | None = None,
        resume_session_id: SubAgentSessionId | None = None,
        subagent_type: str | None = None,
        subagent_name: str | None = None,
        parent_round_index: int | None = None,
        allow_spawn: bool = False,
        disabled_tools: Sequence[str] | None = None,
        spawn_depth: int = 1,
        parent_subagent_id: SubAgentSessionId | None = None,
        parent_abort_check: Callable[[], bool] | None = None,
        parent_consume_cancel_tool: Callable[[str], bool] | None = None,
    ) -> AsyncIterator[SubAgentEvent]:
        """Run the sub-agent and yield SSE-shaped event dicts.

        PR-095 / S9 F-7 sub-agent SSE parity, refined in PR-subagent-stream
        (this PR) to match the V1 wire shape verbatim
        (``backend/chat_handler.py:2204-2343``):

        * ``{"type": "subagent_start", "index": N, "total": M,
          "prompt_preview": "...", "subagent_id": "..."}`` — emitted once at
          entry so the UI can pre-allocate a block (V1 chat_handler.py:598-605).
          ``subagent_id`` (V2, optional) is the resolved resumable id so the
          RUNNING block shows its open/stop affordances immediately; omitted
          when no repo is wired.
        * ``{"type": "subagent_output", "index": N, "content": "..."}``
          — incremental text fragment from the active LLM turn.  Field
          is ``content`` (V1 wire), not ``text``.
        * ``{"type": "subagent_tool", "index": N, "tool_name": "...",
          "tool_args": {...}, "tool_call_id": "..."}`` — emitted after a
          tool call is *dispatched* (mirrors V1's emit-then-execute order);
          ``tool_call_id`` (V2) pairs it to its result event.
        * ``{"type": "subagent_tool_result", "index": N, "tool_name": "...",
          "tool_call_id": "...", "result": "...", "ok": bool}`` — emitted
          AFTER the tool executes (V2 enhancement; neither V1 nor earlier V2
          emitted a sub-agent result event, which let the model re-narrate the
          raw output as plain text). Lets the UI render a structured,
          collapsible result panel under the matching ``subagent_tool`` row,
          identical to a main-agent tool card.
        * ``{"type": "subagent_done", "index": N, "result": "...",
          "rounds": N}`` — terminal event; ``result`` carries the
          consolidated final response (V1 field name).
        * ``{"type": "subagent_error", "index": N, "message": "..."}``
          — fatal error in the sub-agent loop; iteration stops.

        ``agent_index`` (0-based) and ``total_agents`` discriminate
        parallel sub-agents inside one parent turn.  Both default to
        ``0`` / ``1`` for the single-sub-agent case so existing tests
        and the legacy :meth:`execute` wrapper need no changes.

        ``model_hint`` is the model id selected for the *parent* turn.
        It is forwarded onto every sub-agent ``LLMStreamRequest`` so the
        sub-agent routes to the **same** LLM endpoint the parent used
        (V1 parity: ``chat_handler.py:624-632``).  Without it the
        provider-routing stream can't resolve an endpoint and falls back
        to the default LLM (``base_url=None`` on a dev box), which emits
        ``[no LLM endpoint configured]`` — the bug this parameter fixes.

        ``spawn_depth`` (alpha unified-spawn-path — recursion ceiling): the
        recursion depth of the sub-agent about to be spawned. The main agent's
        dispatch passes ``spawn_depth=1`` (a first-level sub-agent under the
        main conversation); when the sub-agent's own agentic loop hits an
        ``agent`` tool call this handler re-enters ``iter_events`` with
        ``spawn_depth=self.depth+1`` (grand = 2, great-grand = 3, …). The
        SESSION persisted for this run records that depth on its ``depth``
        column, so the tree relationship is materialised in the DB rather than
        being lost inside a nested tool-result string. When ``spawn_depth`` is
        already ``>= self._max_spawn_depth`` the caller (``_execute_tool_call``)
        emits a diagnostic string and never even gets here — recursion 封顶
        without a hard ``allow_spawn=False`` in the code path.

        ``parent_subagent_id`` (alpha unified-spawn-path — tree edge): the id
        of the DIRECT parent sub-agent when the caller is itself a sub-agent
        (a grand / great-grand spawn), threaded onto the new session's
        ``parent_subagent_id`` column so the tree edge is persisted. ``None``
        on the main agent's dispatch (the direct parent is the main agent,
        which lives outside the sub-agent table).
        """
        description = request.arguments.get("description", "")
        # V1 also accepts ``prompt`` as the sub-agent prompt argument
        # (chat_handler.py:599 / 613); honour either to match the LLM
        # output of cloud Claude (which emits ``prompt``) and Genie
        # (which historically emitted ``description``).
        if not description:
            description = request.arguments.get("prompt", "")
        if not description:
            yield {
                "type": "subagent_error",
                "index": agent_index,
                "message": "'description' argument is required",
            }
            return

        # V1 prompt-preview cap (chat_handler.py:599): first 500 chars.
        prompt_preview = description[:500]
        # Resolve the sub-agent PROFILE (V2 enhancement). The explicit
        # ``subagent_type`` kwarg (parsed by the caller from the tool_call) wins;
        # if absent, fall back to the value inside the tool_call arguments so a
        # caller that forwards the raw request (without parsing it out) still
        # honours the model's selection. ``resolve_profile`` maps None / unknown
        # / the legacy ``"agent"`` value to GENERAL (historical behaviour).
        effective_type = subagent_type
        if effective_type is None:
            raw_type = request.arguments.get("subagent_type")
            if isinstance(raw_type, str):
                effective_type = raw_type
        # Resolve the sub-agent human-readable NAME (V2 UX enhancement). The
        # explicit ``subagent_name`` kwarg (parsed by the caller from the
        # tool_call) wins; if absent, fall back to the value inside the
        # tool_call arguments so a caller that forwards the raw request
        # (without parsing it out) still honours the model's choice. A
        # missing/empty value ⇒ the UI falls back to ``SubAgent N`` (no
        # regression). Persisted as ``SubAgentSession.title`` and echoed on
        # the ``subagent_start`` frame so the RUNNING card shows the name
        # immediately.
        effective_name = subagent_name
        if effective_name is None or effective_name == "":
            raw_name = request.arguments.get("name")
            if isinstance(raw_name, str) and raw_name:
                effective_name = raw_name
        # Sanitise the LLM-provided label: the UI renders it as a SINGLE-LINE
        # card title, so an embedded ``\n`` / ``\r`` / ``\t`` / other C0
        # control char would break the tab-strip / rail layout (line
        # wrapping in the middle of the header). Strip all C0 controls
        # first, collapse leftover whitespace, then apply the length cap
        # (80 chars ≈ 10 English words / 20 CJK chars — plenty for the
        # "3-5 word label" the schema asks for; a runaway model that stuffs
        # a paragraph in gets clipped rather than derailing the layout).
        # HTML escaping is unnecessary — Vue's ``{{ }}`` interpolation
        # escapes automatically. Falls back to None when everything strips
        # away (empty / whitespace-only) so the UI falls back cleanly.
        if isinstance(effective_name, str):
            effective_name = "".join(
                ch for ch in effective_name if ch == " " or ch >= "\u0020"
            ).strip()
            if len(effective_name) > 80:
                effective_name = effective_name[:80].rstrip()
            if not effective_name:
                effective_name = None
        # NOTE: the ``subagent_start`` event is emitted INSIDE ``_iter_loop``
        # (right after the session is resolved) so it can carry the resolved
        # ``subagent_id`` — letting the UI show the "open in new tab" / "stop"
        # affordances on the RUNNING block immediately, not only after
        # ``subagent_done``. (Previously start was yielded here, before the id
        # existed, so the block had no id until it finished.)

        _log.info(
            "chat.agent_tool.iter_events.start",
            tab_id=str(request.tab_id),
            agent_index=agent_index,
            total_agents=total_agents,
            description=description[:100],
        )

        # Block 2 — independent live-event fan-out. ``_iter_loop`` populates
        # ``broadcast_id`` with the resolved sub-agent id as soon as it knows
        # it (right after ``_resolve_session``); we then publish EVERY event
        # this iterator forwards to the broadcaster keyed by that id, so a
        # standalone sub-agent tab subscribed to
        # ``GET /api/chat/subagents/{id}/stream`` sees live progress. The
        # parent-tab inline forwarding (the ``yield event`` below) is
        # unchanged — this is a SECOND, independent channel. A per-call dict
        # holder keeps this reentrant-safe for parallel sub-agents (no shared
        # ``self`` state). No-op when no broadcaster is wired.
        # ``id`` carries the resolved sub-agent id (== abort id == broadcaster
        # key); ``session`` carries the live RUNNING working aggregate so the
        # anti-leak ``finally`` below can settle it as INTERRUPTED when a
        # cancel/aclose unwinds the run BEFORE ``_iter_loop`` reached its own
        # terminal ``_finalize_session`` (fix B-1 — otherwise the persisted
        # status stays RUNNING and the HTTP snapshot lies).  Typed ``Any`` for
        # the mixed value shapes; only read defensively in the ``finally``.
        broadcast: dict[str, Any] = {"id": None, "session": None}
        bc = self._stream_broadcaster

        def _publish(ev: SubAgentEvent) -> None:
            if bc is None:
                return
            sid = broadcast["id"]
            if sid is None:
                return
            try:
                bc.publish(sid, ev)
                etype = ev.get("type")
                if etype in ("subagent_done", "subagent_error"):
                    bc.mark_terminal(sid)
            except Exception:  # noqa: BLE001 — never block the run on fan-out
                pass

        try:
            async for event in self._iter_loop(
                description=description,
                conversation_id=request.conversation_id,
                tab_id=request.tab_id,
                agent_index=agent_index,
                total_agents=total_agents,
                model_hint=model_hint,
                tool_mode=tool_mode,
                workspace_root=workspace_root,
                resume_session_id=resume_session_id,
                prompt_preview=prompt_preview,
                broadcast=broadcast,
                subagent_type=effective_type,
                subagent_name=effective_name,
                parent_round_index=parent_round_index,
                allow_spawn=allow_spawn,
                disabled_tools=frozenset(disabled_tools or ()),
                spawn_depth=spawn_depth,
                parent_subagent_id=parent_subagent_id,
                parent_abort_check=parent_abort_check,
                parent_consume_cancel_tool=parent_consume_cancel_tool,
            ):
                _publish(event)
                yield event
        except Exception as exc:  # noqa: BLE001 — never propagate
            _log.warning(
                "chat.agent_tool.iter_events.error",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            err_event: SubAgentEvent = {
                "type": "subagent_error",
                "index": agent_index,
                "message": str(exc),
            }
            _publish(err_event)
            yield err_event
        finally:
            # Anti-leak backstop (fix 2): ``_iter_loop`` unregisters this
            # sub-agent's abort flag on its NORMAL completion path
            # (``agent_tool.py`` ~2456, after the terminal ``subagent_done``),
            # but a CANCEL (main-agent Stop cascade → ``gather_task.cancel()``)
            # or a ``GeneratorExit`` (consumer stops iterating) raises through
            # the ``async for`` above BEFORE that line runs, so the flag would
            # leak in ``InMemorySubAgentAbortRegistry._records`` until process
            # restart (and a later interrupt POST would ``set()`` an event no
            # coroutine awaits — a silent no-op). Unregister here so the flag is
            # always dropped. ``unregister`` is idempotent (``pop(..., None)``),
            # so the double call on the normal path is harmless; ``broadcast``
            # carries the resolved id (== ``session.id.value`` == ``abort_id``,
            # set at ``agent_tool.py`` ~1254). Best-effort — never let cleanup
            # mask the exception being unwound.
            _abort_id = broadcast.get("id")
            if self._abort_registry is not None and _abort_id is not None:
                try:
                    self._abort_registry.unregister(_abort_id)
                except Exception:  # noqa: BLE001 — cleanup must never raise
                    pass

            # ── Fix C-1 (ROOT CAUSE) — always mark the broadcaster entry
            # TERMINAL on the way out ──────────────────────────────────────
            # ``_iter_loop`` publishes the terminal ``subagent_done`` /
            # ``subagent_error`` frame and (via ``_publish`` at ~999)
            # ``mark_terminal``s the broadcaster entry on its NORMAL path. But a
            # main/parent Stop cascade cancels the fan-out ``gather_task``
            # (``streaming.py`` ~10088 / ~9785), whose ``CancelledError`` /
            # ``GeneratorExit`` raises through the ``async for`` above BEFORE
            # ``_iter_loop`` reaches that terminal ``yield`` — so the entry's
            # ``done`` flag stayed ``False`` FOREVER. That wedged the whole
            # downstream chain: ``SubAgentStreamBroadcaster._replay_with_entry``
            # dead-waited on ``entry.done`` (never returning), so
            # ``subagent_ws`` never sent ``{type:done}`` / never closed, so the
            # front-end's WS-close → ``refreshFromSnapshot`` settle NEVER fired
            # and the standalone sub-agent tab stayed pinned ``streaming`` /
            # ``aborting`` (floating Stop button never cleared, WS reconnect
            # loop). Marking terminal HERE flips ``entry.done`` on EVERY exit
            # path (normal, cancel, aclose, error) so the replay loop always
            # unwinds and the WS always closes.
            #
            # CANCEL-SAFETY: ``mark_terminal`` is a SYNCHRONOUS, pure in-memory
            # mutation (``sub_agent_stream_broadcaster.py`` ~229: set
            # ``entry.done`` + fan out ``Event.set()``) — no ``await``, so it
            # cannot be interrupted by the cancellation being unwound and cannot
            # introduce a new await-during-cancel hang. IDEMPOTENT: the normal
            # path already marked it (harmless re-set of the flag). Best-effort
            # — cleanup must never mask the exception being unwound.
            if bc is not None and _abort_id is not None:
                try:
                    bc.mark_terminal(_abort_id)
                except Exception:  # noqa: BLE001 — cleanup must never raise
                    pass

            # ── Fix B-1 — settle the persisted session as INTERRUPTED on a
            # cancel that bypassed ``_finalize_session`` ───────────────────────
            # On the same cancel path the session's own terminal
            # ``_finalize_session`` (``agent_tool.py`` ~2647) is also skipped, so
            # the persisted row stays ``RUNNING`` and the HTTP snapshot
            # (``GET /api/chat/subagents/{id}`` → ``detail.status``) lies. The
            # front-end's ``refreshFromSnapshot`` reads that status to decide
            # ``confirmDone`` vs ``confirmAbort``; a stale ``running`` would keep
            # it from settling even after Fix C-1 unblocks the WS. Mark the live
            # working aggregate INTERRUPTED (synchronous domain mutation) and
            # persist it under ``asyncio.shield`` so the in-flight cancellation
            # does not abort the save mid-flight. Only act when the session is
            # still non-terminal (the normal path already settled it → skip, no
            # double-write). Best-effort: a persistence hiccup must never mask
            # the unwind, and a missing repo (legacy/stub) is a clean no-op.
            _session = broadcast.get("session")
            if (
                _session is not None
                and self._sub_agent_sessions is not None
                and not _session.is_terminal()
            ):
                try:
                    # ── Fix B-2 — persist the PARTIAL transcript BEFORE settling
                    # INTERRUPTED so the standalone tab is not blank ────────────
                    # ``record_messages`` requires a non-terminal session, so it
                    # MUST run before ``mark_interrupted`` (identical ordering to
                    # ``_finalize_session``). The loop's own ``_record_round``
                    # only fires at each round BOUNDARY, so a cancel mid-round-1
                    # (before the first boundary) leaves ``session.messages``
                    # EMPTY — the reported "打开子Agent后内容为空" bug. Convert the
                    # LIVE wire snapshot (stashed on ``broadcast``) once via the
                    # SAME shared converter and record it. Only record when there
                    # is genuine content beyond the seed (system + the task user
                    # turn) so we never overwrite a good prior-round snapshot with
                    # a bare seed. Best-effort: a conversion/persist hiccup must
                    # never mask the unwind — fall through to mark_interrupted.
                    _wire = broadcast.get("wire_messages")
                    if isinstance(_wire, list) and len(_wire) > 2:
                        try:
                            _now = self._clock.now()
                            _sigs = broadcast.get("thought_signatures")
                            _structured = _wire_to_structured_messages(
                                _wire,
                                ids=self._ids,
                                now=_now,
                                thought_signatures=(
                                    _sigs if isinstance(_sigs, dict) else None
                                ),
                            )
                            _session.record_messages(
                                messages=_structured,
                                rounds=_session.rounds,
                                now=_now,
                            )
                        except Exception:  # noqa: BLE001 — never mask the unwind
                            pass
                    _session.mark_interrupted(now=self._clock.now())
                    await asyncio.shield(
                        self._sub_agent_sessions.save(_session)
                    )
                    # Align the broadcaster buffer with the just-persisted
                    # snapshot (same checkpoint ``_finalize_session`` does at
                    # ~3038) so a late cursor=0 replay is complementary, not an
                    # echo of the transcript the snapshot now covers.
                    if bc is not None and _abort_id is not None:
                        try:
                            bc.trim_all_published(_abort_id)
                        except Exception:  # noqa: BLE001
                            pass
                except Exception:  # noqa: BLE001 — never mask the unwind
                    pass


    async def _profile_model_override(self, profile_name: str) -> str | None:
        """Best-effort per-profile model id for *profile_name* (or ``None``).

        Reads the injected ``profile_model_overrides_provider`` (a zero-arg
        sync OR async callable returning ``{profile_name: model_id}``) and
        returns the NON-BLANK model id configured for *profile_name*, else
        ``None`` (inherit the parent's ``model_hint``). Read per run so a
        Settings change takes effect on the next dispatch without a restart.

        Any failure — unwired provider, provider raises, non-dict result, a
        missing / non-string / blank entry — yields ``None`` so the run
        degrades to the historical inherit-parent behaviour EXACTLY (never
        blocks a sub-agent run). The domain hard-codes no model; the id (when
        present) always originates from this provider.
        """
        provider = self._profile_model_overrides_provider
        if provider is None:
            return None
        try:
            result = provider()
            if isinstance(result, Awaitable):
                result = await result
        except Exception:  # noqa: BLE001 — override read must never block a run
            return None
        if not isinstance(result, dict):
            return None
        model_id = result.get(profile_name)
        if isinstance(model_id, str) and model_id.strip():
            return model_id
        return None

    def _build_system_text(
        self,
        *,
        workspace_root: str | None,
        model_hint: str | None,
        profile: AgentProfile | None = None,
        has_exec_tools: bool = False,
    ) -> str:
        """Assemble the sub-agent (concise) system prompt.

        Uses the focused ``_SUB_AGENT_SYSTEM_PROMPT`` plus the working-directory
        directive (when the parent resolved a session workspace) + inherited
        AGENTS.md / CLAUDE.md project-context blocks, so the sub-agent's
        file/exec tools default their cwd correctly and it follows the SAME
        project conventions as the main agent.

        ``profile`` (V2 enhancement): when the profile carries a
        ``system_prompt`` override (e.g. ``explore``), it REPLACES the base
        focused prompt — the explorer gets its read-only specialist guidance
        instead of the general sub-agent prompt. The working-directory
        directive + project-context blocks are still appended (so the explorer
        also defaults its cwd and respects project conventions). ``GENERAL`` /
        ``None`` carry no override → the main-agent identity prompt is used
        (regression-safe).
        """
        # Concise assembly is delegated to the SINGLE source of truth for
        # system-prompt assembly: ``RichSystemPromptBuilder.build_sub_agent_concise``
        # (a staticmethod — no builder instance needed, so legacy/stub callers
        # that never wire a builder still work). This module keeps ONLY the
        # sub-agent-specific responsibility the builder must not own: resolving
        # the inherited AGENTS.md / CLAUDE.md project-context files from DISK
        # (CLOUD-only — skipped for a local model hint) and the profile override
        # decision. The builder then assembles the pieces in the exact historical
        # order. Byte-for-byte identical to the pre-refactor inline pipeline.
        #
        # Profile prompt override (explore → its read-only specialist prompt with
        # its own role definition); GENERAL / None reuse the main-agent identity
        # (the "general" sub-agent has no agent.prompt so
        # it falls through to the provider prompt, which starts with the same
        # identity as the main agent).
        base_prompt_override = (
            profile.system_prompt
            if profile is not None and profile.system_prompt is not None
            else None
        )
        # Resolve the inherited project-context files from disk (CLOUD-only):
        # only when a workspace resolved AND the model hint is not local — the
        # exact gating the inline pipeline used before delegating assembly.
        ws = (workspace_root or "").strip()
        ctx_files: list[tuple[str, str]] = []
        if ws and not _is_local_model_hint(model_hint):
            try:
                ctx_files = list(_resolve_workspace_context_files(Path(ws)))
            except (ValueError, OSError):
                ctx_files = []
        return _RichSystemPromptBuilder.build_sub_agent_concise(
            base_prompt_override=base_prompt_override,
            has_exec_tools=has_exec_tools,
            workspace_root=workspace_root,
            is_local_model=_is_local_model_hint(model_hint),
            workspace_context_files=ctx_files,
        )

    async def _forward_passthrough_frame(
        self,
        frame: Any,
        *,
        agent_index: int,
        round_no: int,
    ) -> AsyncIterator[SubAgentEvent]:
        """Adapt a kernel :class:`KernelStreamPassthrough` frame → ``subagent_*``.

        The kernel forwards non-control LLM-stream frames (REASONING thinking
        tokens / cloud ``generating_args`` tool-arg progress / NETWORK_RETRY
        banner) verbatim. The takeover main loop renders these live; the
        sub-agent loop used to DROP them, so a spectator saw no reasoning /
        progress. We translate each into a ``subagent_*`` event so the spectator
        view matches takeover (增强 — the frames were never persisted, only live
        UI, so nothing can regress). Unknown / unmappable frame types yield
        nothing (graceful skip, mirroring the frontend mapper's ``return null``).
        """
        ftype = getattr(frame, "frame_type", None)
        payload = getattr(frame, "payload", None)
        payload = payload if isinstance(payload, dict) else {}
        if ftype is StreamFrameType.REASONING:
            # Model "thinking" tokens → a dedicated reasoning event the frontend
            # routes into the collapsible thinking block (parity with the
            # takeover path's REASONING frame).
            text = payload.get("text", "")
            yield {
                "type": "subagent_reasoning",
                "index": agent_index,
                "content": text if isinstance(text, str) else "",
                "round": round_no,
            }
        elif ftype is StreamFrameType.TOOL_RESULT:
            # Cloud SSE adapter's ``phase="generating_args"`` + ``partial=True``
            # progress frame (a long tool-call argument being streamed). Surface
            # as a tool partial so the spectator sees the args materialise live.
            tcid = payload.get("tool_call_id")
            yield {
                "type": "subagent_tool_partial",
                "index": agent_index,
                "tool_name": payload.get("tool_name") or "tool",
                **({"tool_call_id": tcid} if isinstance(tcid, str) else {}),
                "delta": payload.get("delta")
                if isinstance(payload.get("delta"), str)
                else "",
                "round": round_no,
            }
        # NETWORK_RETRY is deliberately NOT forwarded: the main-agent network-
        # retry banner is driven by the TRANSPORT layer (useChatTransport.ts,
        # `frame.frame_type === "network_retry"` → `store.setNetworkRetry`), NOT
        # by the `applyFrame`/`FRAME_HANDLERS` pipeline that the sub-agent WS
        # feeds. A `subagent_progress` event would therefore have no consumer on
        # the standalone-tab render path (it would fall to the mapper's
        # `return null`). Wiring a sub-agent network-retry banner requires
        # driving `setNetworkRetry` from the sub-agent WS subscription — tracked
        # as a separate enhancement (PENDING-WORK) rather than emitting a
        # dead-end event here. REASONING + generating-args partials ARE wired
        # end-to-end (they reuse existing frame handlers), so 父子统一 holds for
        # the common cases; only the rare mid-run network-retry banner is
        # deferred.
        # Any other frame type → graceful skip (no spectator event), matching
        # the frontend mapper's unknown-event ``return null``.

    async def _iter_loop(
        self,
        *,
        description: str,
        conversation_id: ConversationId,
        tab_id: TabId,
        agent_index: int = 0,
        total_agents: int = 1,
        model_hint: str | None = None,
        tool_mode: str | None = None,
        workspace_root: str | None = None,
        resume_session_id: SubAgentSessionId | None = None,
        prompt_preview: str = "",
        broadcast: dict[str, str | None] | None = None,
        subagent_type: str | None = None,
        subagent_name: str | None = None,
        parent_round_index: int | None = None,
        allow_spawn: bool = False,
        disabled_tools: frozenset[str] = frozenset(),
        spawn_depth: int = 1,
        parent_subagent_id: SubAgentSessionId | None = None,
        parent_abort_check: Callable[[], bool] | None = None,
        parent_consume_cancel_tool: Callable[[str], bool] | None = None,
    ) -> AsyncIterator[SubAgentEvent]:
        """Run the sub-agent agentic loop and yield SSE events.

        Mirrors the legacy ``run_subagent`` structure but emits
        progress incrementally so the parent stream can forward each
        fragment to the UI in real time (V1 chat_handler.py:2188-2343
        parity).

        ``model_hint`` is threaded onto every :class:`LLMStreamRequest`
        below so the sub-agent's LLM turns route to the same endpoint
        the parent turn selected (V1 chat_handler.py:624-632).

        ``resume_session_id`` (V2 enhancement) wakes a PRIOR sub-agent:
        when a :class:`SubAgentSessionRepositoryPort` is wired and the id
        resolves to a stored session, its persisted ``wire_messages`` seed
        this run's history (so the sub-agent continues *with its prior
        memory*) and the new ``description`` is appended as the next user
        turn. When the repo is unwired, the id does not resolve, or no id is
        given, a fresh session is started. Persistence is best-effort and
        entirely skipped when no repo is wired (legacy/stub parity).

        ``subagent_type`` (V2 enhancement) selects the sub-agent PROFILE
        (``general`` default / ``explore`` read-only). For a FRESH session the
        requested profile is resolved and its name persisted onto
        ``SubAgentSession.subagent_type``; for a RESUME the persisted value
        wins (a woken sub-agent keeps the profile it was created with — a
        conflicting passed value is ignored), so an explore sub-agent can never
        be silently upgraded to write access on a follow-up turn.
        """
        # ── Resolve / wake the persisted sub-agent session (V2 enhancement) ──
        # ``session`` is the in-memory working aggregate for THIS run. When a
        # repo is wired we either wake a prior session (seed wire history from
        # its persisted ``wire_messages``) or start a fresh one. ``session`` is
        # None only when no repo is wired (legacy/stub parity — no persistence,
        # behaves exactly as before). The REQUESTED profile name is passed so a
        # fresh session persists it; on a resume the stored value is preserved.
        requested_profile = resolve_profile(subagent_type)
        # Per-profile MODEL override (V2 enhancement) — resolved BEFORE
        # ``_resolve_session`` so a FRESH session persists the CORRECT model as
        # its budget-denominator 真值源 (State-Truth-First 铁律 1/4: the persisted
        # ``session.model_id`` must match the model the sub-agent actually runs,
        # else the context-limit / compaction denominator drifts from reality).
        # For the fresh path the effective profile name IS the requested name
        # (that is what ``_resolve_session`` persists); a resume re-resolves the
        # override against the PERSISTED name below. Blank/missing/unwired
        # override ⇒ the built-in singleton is unchanged and ``model_hint`` is
        # untouched (byte-for-byte the pre-per-profile-model behaviour).
        _requested_override = await self._profile_model_override(
            requested_profile.name
        )
        _requested_profile_resolved = resolve_profile(
            requested_profile.name, model_override=_requested_override
        )
        # Effective model_hint used for spawn/persist: the requested profile's
        # configured model wins; ``None`` inherits the parent turn's model_hint.
        _spawn_model_hint = _requested_profile_resolved.model or model_hint
        session, prior_wire = await self._resolve_session(
            resume_session_id=resume_session_id,
            conversation_id=conversation_id,
            description=description,
            prompt_preview=prompt_preview,
            subagent_type=requested_profile.name,
            title=(subagent_name or ""),
            allow_spawn=allow_spawn,
            model_hint=_spawn_model_hint,
            spawn_depth=spawn_depth,
            parent_subagent_id=parent_subagent_id,
        )

        # Effective profile: the PERSISTED ``subagent_type`` wins when a session
        # is wired (so a resumed session keeps its original profile and a fresh
        # one uses the just-stored requested name); falls back to the requested
        # profile when no repo is wired (legacy/stub parity — no persistence).
        #
        # Per-profile MODEL override (V2 enhancement): for a RESUME whose
        # persisted profile differs from the requested one, re-resolve the
        # override against the PERSISTED name so a resumed explore session picks
        # up the current explore-model setting. For a fresh run (or when the
        # persisted name equals the requested one) reuse the already-resolved
        # ``_requested_profile_resolved`` — avoids a second provider call and
        # keeps the spawn-persisted model consistent with the running model. A
        # blank / missing / unwired override leaves the built-in SINGLETON
        # unchanged (identity-preserving no-op).
        if session is not None and session.subagent_type != requested_profile.name:
            _model_override = await self._profile_model_override(
                session.subagent_type
            )
            profile = resolve_profile(
                session.subagent_type, model_override=_model_override
            )
        else:
            profile = _requested_profile_resolved

        # Effective per-profile MODEL: the profile's configured ``model`` wins;
        # a blank/None ``model`` inherits the parent turn's ``model_hint``. From
        # here down ``model_hint`` IS the sub-agent's model — routed on every
        # ``LLMStreamRequest``, used for local-hint / anthropic-family checks,
        # snapshots, usage. ``None`` on ``profile.model`` ⇒ ``model_hint``
        # unchanged (byte-for-byte the pre-per-profile-model behaviour). For a
        # fresh run this matches ``_spawn_model_hint`` already persisted above,
        # so ``session.model_id`` and the running model agree (State-Truth-First).
        if profile.model:
            model_hint = profile.model

        # Effective per-profile ROUND budget: the profile's static
        # ``max_rounds`` wins; ``None`` inherits the dispatcher's shared
        # ``self._max_rounds`` (GENERAL) so the general path is unchanged.
        effective_max_rounds = (
            profile.max_rounds
            if profile.max_rounds is not None
            else self._max_rounds
        )

        # Tools advertised to the sub-agent's LLM turns (V1 BUILTIN_TOOLS
        # parity, minus ``agent`` for recursion safety). Without this the
        # sub-agent never emits a TOOL_CALL and degrades to a single text
        # turn — it could not read/write/edit/exec to actually do the task.
        # Compose the SAME tool set the main agent uses (minus agent/question),
        # honouring app-builder mode so a cloud app-builder sub-agent also gets
        # ``appbuilder_run`` (identical terms to the main agent).
        # Computed HERE (before ``_build_system_text``) so the system-prompt
        # builder can gate the Python-environment block on whether this run
        # actually advertises exec/background_process (execution capability).
        sub_tools = _sub_agent_tool_schemas(
            self._tool_executor,
            tool_mode=tool_mode,
            is_local=_is_local_model_hint(model_hint),
            profile=profile,
            allow_spawn=allow_spawn,
            disabled_tools=disabled_tools,
            enable_skill=False,
        )
        # Does this run have EXECUTION capability? Reuse the SHARED
        # ``_has_execution_tools`` (single source of truth — main agent, sub-agent
        # and discussion speaker all decide identically; if the execution-tool set
        # ever grows, all three update at once, no drift). Only then is the
        # Python-environment block worth injecting.
        _has_exec_tools = _has_execution_tools({"tools_schemas": sub_tools})

        # Build the sub-agent (concise) system prompt (workspace directive +
        # inherited AGENTS.md/CLAUDE.md project context). Extracted to keep
        # _iter_loop within the branch/statement cohesion budget (§3.6). The
        # profile may override the base prompt (explore → its read-only
        # specialist prompt); GENERAL leaves the prompt unchanged
        # (regression-safe).
        system_text = self._build_system_text(
            workspace_root=workspace_root,
            model_hint=model_hint,
            profile=profile,
            has_exec_tools=_has_exec_tools,
        )

        # Block 2 — register the live-event broadcast entry as soon as the
        # sub-agent id is known (fresh-minted or woken), BEFORE yielding the
        # start event, so ``iter_events``' ``_publish`` also captures
        # ``subagent_start`` into the broadcaster buffer (a standalone tab
        # that backfills then sees the start frame too). No-op when no
        # broadcaster is wired / no repo (legacy/stub parity).
        if (
            broadcast is not None
            and session is not None
        ):
            # Always publish the resolved id into the shared holder as soon as
            # the session resolves — this is the SAME id used for the abort
            # registry (``abort_id``) and the broadcaster key. ``iter_events``'s
            # anti-leak ``finally`` (fix 2) reads it to unregister the abort flag
            # on a cancelled / aclose'd run, so it must be set regardless of
            # whether a broadcaster is wired (previously it was gated behind the
            # broadcaster, so a registry-only wiring leaked the flag on cancel).
            broadcast["id"] = session.id.value
            # Stash the live RUNNING working aggregate so ``iter_events``'
            # anti-leak ``finally`` (Fix B-1) can settle it as INTERRUPTED when a
            # cancel/aclose unwinds this run before its own terminal
            # ``_finalize_session`` runs. Read defensively there (may be a
            # non-terminal RUNNING copy or, on the normal path, an
            # already-settled one that the finally then skips).
            broadcast["session"] = session
            if self._stream_broadcaster is not None:
                try:
                    self._stream_broadcaster.register(session.id.value)
                except Exception:  # noqa: BLE001 — never block the run on fan-out
                    pass

        # Emit ``subagent_start`` HERE (not in ``iter_events``) so it carries
        # the resolved ``subagent_id`` (when persisted). The UI creates the
        # block off this event; carrying the id lets the RUNNING block show the
        # "open in new tab" / "stop" affordances immediately — not only after
        # the sub-agent finishes (``subagent_done`` previously delivered the
        # first id). ``subagent_id`` is omitted when no repo is wired
        # (legacy/stub parity → block stays id-less, exactly as before).
        start_event: SubAgentEvent = {
            "type": "subagent_start",
            "index": agent_index,
            "total": total_agents,
            "prompt_preview": prompt_preview,
        }
        # Tail-appended §3.1 fields (V2 UX enhancement):
        #   * ``subagent_type`` — the RESOLVED profile name (``general`` /
        #     ``explore``); the persisted value wins on a resume so the block
        #     always shows the effective profile, not the requested one. The
        #     UI renders a small i18n type-badge next to the title.
        #   * ``name`` — the LLM-provided human-readable label persisted as
        #     ``SubAgentSession.title``. On a resume the persisted title wins
        #     (so the woken block keeps its original name). Omitted from the
        #     payload when empty — the UI then falls back to ``SubAgent N``.
        if session is not None:
            start_event["subagent_id"] = session.id.value
            start_event["subagent_type"] = session.subagent_type
            if session.title:
                start_event["name"] = session.title
        else:
            start_event["subagent_type"] = profile.name
            if subagent_name:
                start_event["name"] = subagent_name
        # ``round`` (optional, §3.1 tail-appended, V2 UX FIX) — the parent
        # agent's round number at which THIS sub-agent was dispatched.
        # Same key name as ``subagent_output`` / ``subagent_tool`` /
        # ``subagent_tool_result`` events (symmetric), converted to
        # ``round_index`` by the frame factory. Without this the front-end
        # cannot per-round-route SUBAGENT_START and two sub-agents spawned
        # in different rounds of the same parent turn (e.g. A in round 0,
        # B in round 1) both land on the SAME parent message, whereupon
        # B's ``index=0`` de-dup filter drops A's block ("A card
        # disappears when B starts"). Omitted when the caller did not
        # thread a round (legacy path / stub tests) — the front-end then
        # falls back to legacy activeSubAgentMessageId reuse (historical
        # behaviour; the same bug the field fixes).
        if parent_round_index is not None:
            start_event["round"] = parent_round_index
        yield start_event

        # Persist the RUNNING session NOW (early), so the conversation's
        # ``subagent_count`` projection is > 0 the moment a sub-agent spawns —
        # this is what makes the sidebar history row show its expand arrow
        # WHILE the sub-agent is still running (previously the row was only
        # written at ``_finalize_session``, so the arrow appeared only after
        # completion). Best-effort: a persistence hiccup never blocks the run.
        # Resume already has a stored row, so only persist fresh sessions here
        # (avoid an extra CAS bump on a woken session before its first round).
        if (
            session is not None
            and self._sub_agent_sessions is not None
            and resume_session_id is None
        ):
            try:
                await self._sub_agent_sessions.save(session)
            except Exception as exc:  # noqa: BLE001 — never block the run
                _log.warning(
                    "chat.agent_tool.session.early_save_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        # Block 3 — register this sub-agent's independent cancellation flag
        # (keyed by its id) so the interrupt endpoint can stop ONLY this
        # sub-agent. ``abort_event`` is polled between rounds below; cleared
        # in ``finally`` (unregister). No-op when no registry is wired or the
        # session is not persisted.
        #
        # State-Truth-First (AGENTS.md 🔴 铁律 5 — observability): if the
        # registry IS wired but ``session`` is ``None`` (a partial DI wiring
        # bug — the abort registry was injected but the session repository was
        # not, so this sub-agent run goes ahead WITHOUT a persisted session →
        # without a registry record → ``_abort_check`` is hard-wired False and
        # ``_abortable_frames`` skips its 50 ms polling), the entire abort
        # chain silently degrades to a no-op AND the user gets ZERO feedback
        # when they press ⏹. Surface this misconfig as a WARN so an operator
        # sees the broken wiring instead of "Stop button just doesn't work".
        # Control flow stays graceful (no raise) — a partial wiring is still
        # supposed to RUN the sub-agent, just without abort support.
        abort_event: asyncio.Event | None = None
        abort_id: str | None = None
        if self._abort_registry is not None and session is not None:
            abort_id = session.id.value
            # Cascade-abort ownership (runtime-defect fix): record the parent
            # tab that spawned this sub-agent so a Stop on that tab (which only
            # signals ``stream_abort_registry`` keyed by ``tab_id``) can ALSO
            # cascade into this sub-agent via
            # ``subagent_abort_registry.abort_by_owner_tab(tab_id)`` — closing
            # the two-registry gap that let an autonomously-spawned sub-agent
            # run forever after the user pressed ⏹ on the main agent tab.
            abort_event = self._abort_registry.register(
                abort_id, owner_tab_id=tab_id.value
            )
        elif self._abort_registry is not None and session is None:
            _log.warning(
                "chat.subagent.abort_registry_wired_but_no_session — abort "
                "chain DEGRADED to no-op (Stop button will be silently "
                "ineffective for this sub-agent run). Check DI wiring: "
                "``sub_agent_sessions`` (SqliteSubAgentSessionRepository) "
                "must be injected alongside ``subagent_abort_registry`` "
                "(InMemorySubAgentAbortRegistry). See apps/api/_chat_di.py.",
            )
        # Fix 2 (immediate parent-Stop perception): a merged cooperative-abort
        # handle that fires when EITHER this sub-agent's own registry event is
        # set (standalone tab ⏹ / manual interrupt endpoint / the Fix-1 owner
        # cascade) OR the PARENT tab's stream-abort handle is set
        # (``parent_abort_check`` — the main agent tab's ⏹, threaded down from
        # the spawning ``StreamChatUseCase`` turn). Without the parent leg the
        # sub-agent could only see its own registry event, which the main tab
        # Stop never set directly (it relied on the Fix-1 cascade / task
        # cancellation). With it, the sub-agent's round loop + per-round LLM
        # stream guards + parallel-tool abort race ALL observe the parent Stop
        # the instant it lands — so the loop stops at the next round top even
        # if the cascade映射 were ever incomplete. State-Truth-First: NEVER
        # reports aborted when neither source is set (no false abort — the
        # parent leg is a plain read of the parent's own ``is_set``).
        merged_abort = _MergedAbortHandle(
            event=abort_event,
            parent_abort_check=parent_abort_check,
        )
        # ── OpenAI wire-history (the SAME shape the parent agentic loop uses
        # via ``extra["messages"]`` — ``streaming.py:_build_base_wire_messages``
        # + ``_append_tool_round`` 2500-2630). The earlier version accumulated
        # domain ``Message`` objects and threaded them through
        # ``LLMStreamRequest.history``; the cloud adapter's
        # ``_normalize_history_message`` (llm_stream.py:234-242) then dropped
        # every ``tool_calls`` / ``tool_results`` field (it only kept role +
        # content for the ``Message`` shape), so round ≥2 sent an
        # ``assistant`` with NO ``tool_calls`` followed by an orphan
        # ``role:tool`` — which ``sanitize_tool_messages`` then deleted
        # (message_sanitizer.py:241-250). The model lost both the executed
        # tool RESULT and (because the prompt had been replaced with
        # "Continue with the task.") the ORIGINAL task, and answered
        # "what would you like me to do?" (the reported sub-agent
        # answers-the-wrong-question bug).
        #
        # Maintaining a wire dict array instead — kept verbatim by both the
        # cloud (llm_stream.py:1145-1147) and local
        # (local_model_stream.py:813-843) adapters — preserves the
        # ``assistant{tool_calls:[{id}]}`` → ``tool{tool_call_id}`` pairing
        # AND keeps the original task user turn present every round.
        #
        # On a WAKE (prior_wire non-empty): the seed is the prior session's
        # wire history (system + earlier task/tool turns) plus the NEW task as
        # the next user turn, so the woken sub-agent continues WITH its memory.
        # Defensive: if the persisted prior wire has no leading system turn
        # (older data, or a session that was stripped before this fix), prepend
        # the freshly-built system prompt so a woken sub-agent never runs
        # without its system guidance.
        if prior_wire:
            seeded: list[dict[str, Any]] = list(prior_wire)
            if not (seeded and seeded[0].get("role") == "system"):
                seeded.insert(0, {"role": "system", "content": system_text})
            wire_messages: list[dict[str, Any]] = [
                *seeded,
                {
                    "role": "user",
                    "content": description,
                    # Display-only per-turn timestamp (V2): lets the front-end
                    # show the REAL time each turn occurred (parity with the
                    # main agent's per-Message created_at) instead of synthesising
                    # all turns from one base. Stripped before the wire is sent
                    # to the model (see ``send_messages``).
                    "created_at": self._clock.now().isoformat(),
                },
            ]
        else:
            wire_messages = [
                {"role": "system", "content": system_text},
                {
                    "role": "user",
                    "content": description,
                    "created_at": self._clock.now().isoformat(),
                },
            ]

        # P0 #10: repair orphan tool messages (role:tool without a matching
        # assistant tool_call, or vice-versa) before the first LLM turn.
        # Mirrors streaming.py:5833 (normal history rebuild) and :3217
        # (take-over prior_wire). On a resume, prior_wire may carry orphans
        # from a prior interrupted run; on a fresh start this is a no-op.
        wire_messages = _repair_orphan_tool_messages(wire_messages)

        # ── Fix B-2 (empty-transcript on cascade-cancel) — expose the LIVE wire
        # to ``iter_events``' anti-leak ``finally`` so a cancel that bypasses
        # ``_finalize_session`` can still persist WHATEVER partial transcript
        # exists BEFORE marking the session INTERRUPTED. ``record_messages``
        # requires a non-terminal session, so the finally MUST record first,
        # then mark_interrupted (same ordering ``_finalize_session`` uses).
        # Without this, a sub-agent aborted mid-round-1 (before ``_record_round``
        # first fires) persisted an EMPTY ``session.messages`` → the standalone
        # tab opened blank. The value is a LIVE reference to the same list the
        # loop mutates in place, so the finally always sees the latest turns.
        broadcast["wire_messages"] = wire_messages

        # Number of wire turns that existed BEFORE this run (the seed: system +
        # prior task/tool turns on a wake, or system + the fresh user turn). Used
        # after the loop to stamp per-round request_id/usage onto ONLY this run's
        # newly-appended assistant turns (a woken session's prior turns keep
        # whatever they already carried).
        _seed_len = len(wire_messages)

        # (``sub_tools`` is computed EARLIER — right before ``_build_system_text``
        # — so the system-prompt builder can decide whether to inject the Python
        # environment block based on whether exec/background_process are actually
        # advertised this run. See the assignment above.)

        # P9 — per-run :class:`GuardrailPort` (parity with the main agent's
        # ``StreamChatUseCase``: each turn gets its own fresh guardrail
        # instance so the controller's per-turn accumulators do not leak
        # across runs). ``None`` when the factory is unwired — the
        # tool-call wrapper degrades to the pre-P9 behaviour (profile
        # deny-list + per-session disabled-tools remain authoritative).
        guardrail: GuardrailPort | None = (
            self._guardrail_factory()
            if self._guardrail_factory is not None
            else None
        )

        accumulated_text: list[str] = []
        rounds_done = 0
        interrupted = False
        # Block 4 — rounds accumulate ACROSS resumes (the persisted ``rounds``
        # is the cumulative total of all runs of this sub-agent, not just the
        # current run). The loop budget (``self._max_rounds``) still resets to
        # a full allowance each resume — the two are deliberately separate: the
        # budget bounds THIS run, the persisted counter records lifetime work.
        # ``base_rounds`` is the prior cumulative total (0 for a fresh run; the
        # woken session's ``rounds`` on a resume).
        base_rounds = session.rounds if session is not None else 0

        # P1 #9 — cache-breakpoint anchor state (mirrors streaming.py:8610-8701).
        # Local state replaces the per-conversation process-level dicts the main
        # agent uses; the sub-agent run is ephemeral so local state suffices.
        #   _sub_aged_lb   : monotonic lower-bound on the oldest-aged-run count
        #                    (only ever increases → frozen prefix stays byte-stable)
        #   _sub_cache_anchor: frozen count forwarded to the adapter as
        #                    __cache_stable_aged_prefix__; re-based when compaction
        #                    fires THIS round (detected by _compact_hook firing —
        #                    a DIRECT signal, not the old indirect "wire shrank"
        #                    inference which missed the rare no-shrink compaction;
        #                    State-Truth-First: use the真值 that compaction ran).
        #   _sub_compaction_fired: set True by _compact_hook when it rebuilds the
        #                    wire this round; consumed (reset) by _open_round_stream
        #                    on the next round. The kernel runs compact_hook (②)
        #                    BEFORE open_round_stream (④) in the same round
        #                    (_single_agent_turn.py:695-757), so the flag is
        #                    accurate when read.
        _sub_aged_lb: int = 0
        _sub_cache_anchor: int = 0
        _sub_compaction_fired: bool = False

        # Per-round prompt-snapshot bookkeeping (回看 parity with the main
        # agent). ``_round_request_ids`` maps a kernel round_no → the
        # ``request_id`` minted when that round's snapshot was saved to the
        # shared store; ``_round_usages`` maps round_no → that round's provider
        # token-usage dict. After the loop these are stamped onto the persisted
        # assistant turn of each round so ``_wire_to_messages`` can surface a
        # per-message 📄 button + token line (IDENTICAL to the main agent). A
        # stable ``turn_ref`` lets the shared store keep ONE list per turn (O(N)
        # — every round's wire is a prefix of the next), exactly like the main
        # agent's ``_save_round_snapshot``.
        _round_request_ids: dict[int, str] = {}
        _round_usages: dict[int, dict[str, Any]] = {}
        _snapshot_turn_ref = uuid.uuid4().hex

        # ── Differential-compaction bookkeeping (byte-for-byte the main agent's
        # ``completed_rounds`` + ``last_round_usage`` seam) ───────────────────
        # ``completed_rounds`` accumulates ONE ``_SubAgentCompletedRound`` per
        # TOOL round (the no-tool finishing round produces none, exactly like
        # the main agent — a terminal round has no atomic group to attribute).
        # The shared :class:`CompactionCheckpointEngine` reads it via duck-typing
        # for per-group real-token attribution. ``_last_round_usage`` holds the
        # MOST-RECENT round's provider usage so ``_compact_hook`` can thread the
        # measured ``实发`` into the trigger gate (mid-turn this usage is not yet
        # on any persisted message — State-Truth-First: the size actually being
        # sent THIS turn is the correct compaction judgement). ``_pending_round``
        # carries one round's text + tool_calls + usage from ``_on_round_end``
        # (which fires BEFORE tool execution) to ``_on_tool_round_complete``
        # (which fires AFTER the wire is grown — so the round's ``role:tool``
        # replies exist and can be paired into ``tool_results``). A nested dict
        # holder keeps this reentrant per-run (no shared ``self`` state — the
        # handler fans out parallel sub-agents).
        completed_rounds: list[_SubAgentCompletedRound] = []
        _last_round_usage: dict[str, dict[str, Any] | None] = {"usage": None}
        _pending_round: dict[int, dict[str, Any]] = {}
        # Full-unification (Step 3): accumulate Vertex AI thought_signatures
        # (call_id → signature) from the raw TOOL_CALL frame payloads captured
        # in ``_on_round_end``. The kernel's built-in wire-growing path now
        # writes thought_signature onto the wire's ``assistant.tool_calls[i]``
        # (``_single_agent_turn.py``:843-860), so the send wire carries it
        # directly. We still capture it here as a redundant safety net so
        # ``_wire_to_structured_messages`` can fold it onto the structured card
        # at finalize time even if a wire entry ever lacks it (double-cover) —
        # making the sub-agent's structured transcript signature-lossless
        # (AGENTS.md 拍板 "存签名").
        _thought_signatures: dict[str, str] = {}
        # Fix B-2 (cont.): also expose the live signature map so the anti-leak
        # ``finally`` records a signature-lossless partial transcript on cancel
        # (same double-cover the terminal ``_finalize_session`` uses). Live
        # reference — the loop mutates this dict in place as rounds complete.
        broadcast["thought_signatures"] = _thought_signatures
        # Task B (State-Truth-First): the live, provider-measured used-token
        # count for THIS sub-agent's running context. The standalone sub-agent
        # tab's LIVE stream previously carried NO token figure, so its context
        # badge froze at round 1's value until the run finished and the GET
        # snapshot refreshed it (the reported "运行中 token 不刷新" bug). We now
        # update this holder from ``session.last_prompt_tokens`` AFTER each
        # round's ``accumulate_usage`` (the真实 wire size that round measured —
        # NOT an estimate) and append it (+ the model's ``context_limit``) as a
        # tail field to the round's stream events, so the front-end updates the
        # badge in real time every round. ``None`` until the first round with
        # usage lands; stays ``None`` for no-usage (local / stub) runs (the
        # field is then simply omitted — no regression).
        _live_used_tokens: dict[str, int | None] = {"used": None}
        # Reflect the EXACT wire each round sends (State-Truth-First): the
        # cloud LLM adapter advertises ``tool_choice="auto"`` alongside
        # ``tools`` (mirrors the main agent's snapshot口径 —
        # ``streaming.py:6155-6158``); local (on-device) turns do NOT send
        # ``tool_choice``. So stamp it onto the snapshot options only for
        # cloud sub-agent turns, matching what actually went over the wire.
        if sub_tools:
            _snapshot_request_options = {"tools": list(sub_tools)}
            if not _is_local_model_hint(model_hint):
                _snapshot_request_options["tool_choice"] = "auto"
        else:
            _snapshot_request_options = None

        # ── Round skeleton via the SHARED kernel (§15) ────────────────────
        # The kernel owns the per-round skeleton (abort check → compress →
        # build send wire → drain & classify → assistant{tool_calls} +
        # parallel execute + paired role:tool replies → grow wire → cap). This
        # shell injects the sub-agent specifics and adapts each neutral
        # ``KernelEvent`` into the verbatim ``subagent_*`` wire shape, and keeps
        # ALL persistence / broadcaster / abort markers (§15.2 务实边界). The
        # cooperative interrupt is the kernel's ``abort_check`` (V1 parity: stop
        # BEFORE the next LLM turn; the in-flight round runs to completion).

        def _open_round_stream(
            *, round_no: int, send_wire: list[dict[str, Any]]
        ) -> AsyncIterator[StreamFrame]:
            nonlocal _sub_aged_lb, _sub_cache_anchor, _sub_compaction_fired
            # Hand the FULLY-ASSEMBLED wire down via ``extra["messages"]`` (the
            # same contract the parent loop uses). The kernel already blanked
            # the ``[tool_calls]`` sentinel + stripped ``created_at`` on the
            # send copy. ``prompt`` / ``history`` are unused (extra takes over).
            #
            # 子 agent wire 老化（对齐主 agent streaming.py 已验证的机制）：
            # ``send_wire`` 是内核 ``build_send_wire`` 产出的独立新 list
            # (_single_agent_turn.py:747)，与持久化 ``wire_messages`` (定义于
            # :1387、由 ``_record_round`` :1881 持久化) 是两个不同对象；
            # ``age_old_tool_outputs`` 返回新 list、不 mutate 入参。只清最近 6 条
            # 之外的旧 tool 结果 content（role/tool_call_id 因果链保留，完整结果
            # 落盘 data/tool_results/ 可 read 取回），最近 6 条永远保留原文。
            #
            # 方案B gate: only age when the shared registry says this gateway has
            # NO prompt cache (or no registry wired = prior unconditional aging).
            # Unknown (first round) / supports-cache → SKIP aging so the cached
            # prefix stays byte-clean; no-cache → age for real byte savings.
            if (
                self._provider_cache_registry is None
                or self._provider_cache_registry.aging_enabled(model_hint)
            ):
                send_wire = age_old_tool_outputs(
                    send_wire,
                    model_hint=model_hint,
                    keep_recent_tool_results=6,
                    protect_tokens=24_000,
                    # P1 #9: enforce the monotonic lower-bound so the frozen
                    # prefix stays byte-stable across rounds (mirrors
                    # streaming.py:8592 min_aged_tool_count).
                    min_aged_tool_count=_sub_aged_lb or None,
                )
            # P0 #10: repair orphan tool messages on every round's send_wire
            # (the per-round choke point, mirrors streaming.py:6196).
            # age_old_tool_outputs returns a new list; repair also returns a
            # new list — neither mutates the persistent wire_messages.
            send_wire = _repair_orphan_tool_messages(send_wire)

            # P1 #9: compute cache-breakpoint anchor (mirrors streaming.py:
            # 8610-8701). Count the contiguous oldest aged-placeholder
            # role:tool messages at the head of send_wire.
            _aged_run: int = 0
            for _m in send_wire:
                if not (isinstance(_m, dict) and _m.get("role") == "tool"):
                    continue
                if _m.get("content") == AGED_TOOL_OUTPUT_PLACEHOLDER:
                    _aged_run += 1
                else:
                    break
            # Advance the monotonic lower-bound (only grows).
            _sub_aged_lb = max(_sub_aged_lb, _aged_run)
            # Re-base the anchor when compaction fired THIS round (a DIRECT
            # signal from _compact_hook, set before this stream opens — the
            # kernel runs compact_hook then open_round_stream in one round) or
            # on the first observation. This replaces the earlier indirect
            # "wire_messages got shorter" heuristic, which missed the rare case
            # where a compaction's summary+tail was not shorter than the head
            # it replaced (State-Truth-First: use the fact that compaction ran,
            # not a proxy for it).
            if _sub_compaction_fired or _sub_cache_anchor == 0:
                _sub_cache_anchor = _aged_run
                _sub_compaction_fired = False  # consume the one-shot flag
            # First-aging freeze: once aging first produces placeholders,
            # freeze the baseline (mirrors streaming.py:8667-8671).
            if _sub_cache_anchor <= 0 and _aged_run > 0:
                _sub_cache_anchor = _aged_run
            # Forward the anchor to the adapter (only when > 0).
            _anchor = _sub_cache_anchor if _sub_cache_anchor > 0 else _aged_run

            sub_extra: dict[str, Any] = {"messages": send_wire}
            if _anchor > 0:
                sub_extra["__cache_stable_aged_prefix__"] = _anchor
            if sub_tools:
                sub_extra["tools_schemas"] = sub_tools
            # Sampling-parameter injection (family-default ``max_tokens`` +
            # temperature / top_p locks) is now done in ONE place —
            # ``ProviderRoutingLLMStream._select_target`` ->
            # ``_inject_family_sampling_defaults`` — through which EVERY routed
            # ``LLMStreamRequest`` (main agent, this sub-agent, discussion
            # speaker) passes. The sub-agent therefore no longer re-derives the
            # family default here: the previous local copy
            # (``get_model_profile(...).resolve_max_tokens(user_value=None)``
            # gated on ``_is_local_model_hint``) was duplicated logic that drifted
            # from the main agent (it only injected ``max_tokens``, never the
            # GPT-5 / o-series temperature lock — so a family-locked sub-agent
            # turn could 400). The unified收口 covers all three: cloud opus still
            # gets 16384, local still skips ``max_tokens``, query::/unknown still
            # stay unset. See ``test_provider_routing_sampling_defaults.py``.
            stream_request = LLMStreamRequest(
                conversation_id=conversation_id,
                tab_id=tab_id,
                prompt=_EMPTY_PROMPT,
                history=(),
                extra=sub_extra,
                model_hint=model_hint,
            )

            # P4 — wrap with the SAME guards the main agent uses
            # (``StreamChatUseCase._network_retrying_stream`` +
            # ``_abortable_frames`` now delegate to the SAME free helpers in
            # ``_stream_guards.py``). Without these the sub-agent's per-round
            # stream was bare ``self._llm.stream(...)`` → a user Stop on a
            # half-open cloud SSE could not interrupt until the 600s httpx
            # read-timeout AND a transient network glitch crashed the whole
            # run instead of auto-retrying. ``abort_event`` (an
            # ``asyncio.Event``) satisfies the duck-typed handle protocol
            # (only ``is_set()`` is called inside the helpers).
            #
            # When the optional dependencies are unwired
            # (``self._retry_policy is None`` AND no ``abort_event``), the
            # wrappers degrade gracefully:
            #   * ``network_retrying_stream`` is a pass-through;
            #   * ``abortable_frames`` is skipped (no handle).
            #
            # Fix 2: use the MERGED handle (own registry event OR parent tab
            # Stop) so a mid-stream parent Stop interrupts a half-open cloud
            # SSE the same way the sub-agent's own ⏹ does. ``None`` only when
            # NEITHER an ``abort_event`` NOR a ``parent_abort_check`` is wired
            # (legacy / unit-stub parity — pre-fix behaviour byte-for-byte).
            handle = (
                cast(StreamAbortHandle, merged_abort)
                if merged_abort.active
                else None
            )

            def _open() -> AsyncIterator[StreamFrame]:
                return self._llm.stream(stream_request)

            def _build_retry_frame(
                attempt: int, delay_seconds: float, code: Any
            ) -> StreamFrame:
                return StreamFrame.network_retry(
                    frame_id=f"sub-net-retry-{self._ids.new_id()}",
                    sequence=0,
                    attempt=attempt,
                    delay_seconds=delay_seconds,
                    code=code if isinstance(code, str) else None,
                )

            retrying = _network_retrying_stream(
                _open,
                handle,
                retry_policy=self._retry_policy,
                classify_error_frame=_classify_error_frame,
                build_retry_frame=_build_retry_frame,
                sleep=self._sleep,
                scope="sub_agent_round",
            )
            if handle is None:
                # No abort handle → cannot meaningfully wrap; the kernel's
                # per-frame ``abort_check`` is the only abort signal left
                # (matches the pre-P4 behaviour byte-for-byte when no
                # ``abort_registry`` was wired).
                return retrying
            return _abortable_frames(
                retrying,
                handle,
                frame_stall_budget_s=self._frame_stall_budget_s,
            )

        def _tool_executor(
            *,
            round_no: int,
            tool_metas: list[tuple[str, dict[str, Any], str]],
        ) -> AsyncIterator[ToolExecutionItem]:
            # Execute all tool calls in PARALLEL via the SHARED round skeleton
            # (``execute_tools_in_parallel_stream`` — same concurrency + per-call
            # timer + abort racing + single-tool cancel the main agent uses, but
            # PER-SLOT: each slot is yielded the instant it finishes rather than
            # after a whole-round ``gather`` barrier, so a cancelled/fast tool
            # card flips immediately instead of waiting for the slowest call —
            # 父子统一 with the takeover path's per-slot dispatch). Truncate each
            # result via the SHARED model-aware truncator, then yield ONE FINAL
            # item per call (order may interleave; the kernel re-pairs by
            # ``call_id`` so the wire fed back to the model stays in issue order).
            #
            # Bilingual single-tool cancel message language (父子统一 with
            # takeover ``streaming._cancelled_tool_message``): a coarse CJK check
            # on the sub-agent's task ``description`` (the sub-agent has no
            # ``request.user_message`` here; ``description`` is the task text that
            # seeded this run). Good enough for a short status line.
            _sub_is_zh = any(
                "\u4e00" <= _ch <= "\u9fff" for _ch in (description or "")
            )

            async def _gen() -> AsyncIterator[ToolExecutionItem]:
                # L3 — live exec partials sink. ``_run_one`` (below) forwards
                # each streaming STDOUT/STDERR increment to ``on_partial``, which
                # enqueues a ``ToolExecutionItem(partial=True)`` here; the driver
                # loop drains this queue interleaved with the per-slot FINALS so
                # the spectator streams the tool card's output live (父子统一 with
                # the takeover path). The kernel forwards partials as
                # ``KernelToolPartial`` and NEVER folds them into the wire, so
                # the LLM-facing result is exactly the FINAL only (no change to
                # what the model sees).
                _partials: asyncio.Queue[ToolExecutionItem] = asyncio.Queue()

                async def _run_one(
                    name: str, args: dict[str, Any], _call_id: str
                ) -> Any:
                    def _on_partial(delta: str) -> None:
                        # Best-effort, non-blocking enqueue (unbounded queue →
                        # ``put_nowait`` never raises here). Runs in the same
                        # event loop as the driver, so ordering per call is
                        # preserved.
                        _partials.put_nowait(
                            ToolExecutionItem(
                                call_id=_call_id,
                                tool_name=name,
                                arguments=args,
                                partial=True,
                                delta=delta,
                                result_text=delta,
                            )
                        )

                    return await self._execute_tool_call(
                        tool_name=name,
                        arguments=args,
                        tab_id=tab_id,
                        conversation_id=conversation_id,
                        profile=profile,
                        allow_spawn=allow_spawn,
                        spawn_model_hint=model_hint,
                        spawn_tool_mode=tool_mode,
                        spawn_workspace_root=workspace_root,
                        disabled_tools=disabled_tools,
                        guardrail=guardrail,
                        # Unified spawn-path (alpha): THIS sub-agent's depth is
                        # the AUTHORITATIVE truth source — take it from the
                        # persisted ``session.depth`` (an identity property of
                        # the node preserved across wake / take-over — see
                        # ``_resolve_session``'s wake branch), NOT from the
                        # caller-passed ``spawn_depth``. Those two are equal on
                        # a FRESH spawn but can drift on a wake: a resumer may
                        # pass ``spawn_depth=N`` while the awoken row has its
                        # OWN persisted depth (from the original spawn),
                        # because the tree is an identity property of the
                        # node — a wake never restructures it. When no repo is
                        # wired (``session is None``) we have no identity truth
                        # to consult and fall back to ``spawn_depth`` (fresh-
                        # spawn parity — the light stub path never touches
                        # persistence anyway). ``current_subagent_id`` is the
                        # id of THIS running session so a nested ``agent`` call
                        # records its tree edge (``parent_subagent_id`` = my
                        # id). ``current_parent_round_index`` is the caller's
                        # round number so a nested spawn's SUBAGENT_START
                        # frame carries ``round`` for broadcaster fan-out
                        # (parity with the main agent's dispatch).
                        spawn_depth=(
                            session.depth if session is not None else spawn_depth
                        ),
                        current_subagent_id=(
                            session.id if session is not None else None
                        ),
                        current_parent_round_index=round_no,
                        # Fix 2: forward the parent-Stop predicate so a nested
                        # (grand) spawn started from THIS sub-agent's tool round
                        # also observes the originating tab's ⏹.
                        parent_abort_check=parent_abort_check,
                        # Single-tool cancel: forward the parent tab's per-tool
                        # cancel check so a nested spawn's own tool cards can be
                        # stopped individually too.
                        parent_consume_cancel_tool=parent_consume_cancel_tool,
                        # L3: live exec partials → spectator (see ``_on_partial``).
                        on_partial=_on_partial,
                    )

                # ROOT-CAUSE FIX (子 Agent tool round 挂死 / 点停止按钮不恢复):
                # ``StopChatUseCase`` is a pure flag-setter (it does NOT cancel
                # the asyncio task running this round — streaming.py
                # ``StopChatUseCase.execute``). A bare ``await
                # _execute_tools_in_parallel(...)`` therefore had NO task-level
                # cancel: a long/hung ``exec`` (the裸 subprocess handler is only
                # sensitive to a task CancelledError) kept the ``asyncio.gather``
                # inside the skeleton pending forever, so this ``_gen()`` never
                # yielded, the kernel blocked at ``async for item in
                # tool_executor`` (_single_agent_turn.py) and never reached its
                # between-rounds ``abort_check`` → the sub-agent turn hung and
                # never emitted a terminal frame (frontend Stop button stuck).
                #
                # We now run the skeleton as a CANCELLABLE task and poll the
                # MERGED abort handle (``merged_abort`` — this sub-agent's own
                # registry event OR the PARENT tab's Stop; the old code passed
                # only ``abort_event`` to the skeleton's race, so a parent-tab
                # Stop that did not set THIS sub-agent's event was invisible to
                # the round). On abort we ``cancel()`` the task: the skeleton's
                # outer-cancel handler (_tool_round_executor.py) cancels + reaps
                # each in-flight call, which propagates the cancel into
                # ``exec.py`` where the subprocess TREE is killed (exec.py) —
                # so the child dies IMMEDIATELY, not at its timeout. We then
                # await the task to let that tree-kill+reap COMPLETE before we
                # synthesise ``[interrupted]`` finals and return, so the round
                # ends bounded and the kernel's next-round ``abort_check``
                # (= merged_abort.is_set) fires ``KernelAborted`` promptly.
                #
                # The skeleton's own ``abort_event`` race is KEPT (it still
                # yields per-call ``interrupted`` results on a fast, in-band
                # abort without a task cancel); this task-cancel is the failsafe
                # for a call that is wedged in a cancel-only tool like ``exec``.
                # PER-SLOT streaming (父子统一 with the takeover path): consume
                # the shared skeleton's async-generator variant so each tool's
                # FINAL item is yielded the INSTANT that slot finishes (or is
                # per-tool cancelled) rather than after a whole-round barrier —
                # a cancelled/fast card flips immediately instead of waiting for
                # the slowest call. The generator keeps ALL the prior semantics
                # (concurrency + budget + ``abort_event`` racing + single-tool
                # ``cancel_check`` + ``return_exceptions`` parity + outer-cancel
                # tree-kill reap). The kernel re-pairs finals by ``call_id`` so
                # the wire fed to the model stays in issue order (乱序 yield is
                # safe — ``_single_agent_turn._execute_tool_round``).
                _stream = _execute_tools_in_parallel_stream(
                    tool_metas=tool_metas,
                    run_one=_run_one,
                    abort_event=abort_event,
                    concurrency=self._tool_concurrency,
                    # Single-tool cancel (子 Agent 工具卡右上角停止按钮): the
                    # frontend's ``cancel_tool(tab_id, call_id)`` marks ONE call
                    # on the PARENT tab's stream-abort handle. The skeleton polls
                    # ``consume_cancel_tool(call_id)`` per call and, on a hit,
                    # cancels ONLY that call's task (→ exec.py tree-kill) marking
                    # it ``[cancelled]`` while the OTHER calls keep running and
                    # the round continues (NOT a whole-turn abort → that stays
                    # ``abort_event`` / Stop). ``None`` when no parent handle is
                    # wired (standalone tab / light stub) → whole-round abort-
                    # only behaviour (no regression).
                    cancel_check=parent_consume_cancel_tool,
                )

                def _shape_final(slot: Any) -> ToolExecutionItem:
                    # Turn one skeleton ``SlotResult`` into a FINAL
                    # ``ToolExecutionItem`` (truncate + ok/cancel classification).
                    truncated = self._truncate_sub_tool_result(
                        slot.raw, tool_name=slot.tool_name, model_hint=model_hint
                    )
                    if slot.interrupted:
                        # User Stop / per-tool cancel won the race for this call.
                        # Surface a DISTINCT, non-error signal (not
                        # ``[tool_error]``) so the LLM understands the call was
                        # cancelled by the user rather than failing on its own.
                        # ``ok=False`` so it is not treated as a successful
                        # result. Distinguish the single-tool ``cancel_tool``
                        # (``[cancelled]`` — turn continues) from the whole-round
                        # abort (``[interrupted]``) by the skeleton's marker on
                        # ``slot.raw`` so the model reads the accurate cause.
                        # 父子统一: for the single-tool cancel use the SHARED
                        # bilingual message (same text the takeover path shows +
                        # feeds the model) and mark ``cancelled=True`` so the
                        # spectator/takeover UI tags the card "已取消/cancelled".
                        _marker = str(slot.raw)
                        _is_cancel = _marker.startswith("[cancelled]")
                        if _is_cancel:
                            _result_text = _build_cancelled_tool_message(
                                is_zh=_sub_is_zh
                            )
                        else:
                            _result_text = (
                                "[interrupted] tool call aborted by user"
                            )
                        _ok = False
                        _cancelled = _is_cancel
                    else:
                        _result_text = (
                            str(truncated)
                            if not isinstance(truncated, BaseException)
                            else f"[tool_error] {truncated}"
                        )
                        _ok = not (
                            isinstance(slot.raw, BaseException)
                            or _result_text.startswith("[tool_error]")
                            or _result_text.startswith("[guardrail_blocked]")
                        )
                        _cancelled = False
                    return ToolExecutionItem(
                        call_id=slot.call_id,
                        tool_name=slot.tool_name,
                        arguments=slot.arguments,
                        partial=False,
                        result_text=_result_text,
                        ok=_ok,
                        duration_ms=slot.duration_ms,
                        cancelled=_cancelled,
                    )

                # Poll interval mirrors the shared abort-poll cadence used by
                # ``_abortable_frames`` (50 ms) so Stop feels instant without
                # burning CPU.
                _ABORT_POLL_S = 0.05
                _seen_call_ids: set[str] = set()
                _agen = _stream.__aiter__()
                # ORPHAN-KILL FIX (symmetry with the takeover path): drive the
                # per-slot stream with an abort poll. A single PERSISTENT
                # ``__anext__`` future is parked on ``asyncio.wait(timeout=...)``
                # (which does NOT cancel it on timeout) so between polls we
                # re-check the merged abort flag WITHOUT ever creating a second
                # concurrent ``__anext__`` on the same generator (illegal). An
                # OUTER cancel (parent fan-out ``gather_task.cancel()`` / stall
                # budget) OR the merged abort flag being set tears the round
                # down: the ``finally`` ``aclose()``s the generator, which runs
                # ITS reap (cancel + reap every pending slot task → exec.py
                # tree-kill, no orphaned child); on a cooperative abort we then
                # emit a bounded ``[interrupted]`` final for each un-yielded
                # call so the kernel pairs replies by call_id (cards never stuck
                # "executing").
                _aborted_round = False
                _next_task: asyncio.Task[ToolExecutionItem] | None = None
                try:
                    while True:
                        # Drain any live exec partials queued since the last
                        # iteration FIRST so the spectator streams the tool card
                        # output the instant each STDOUT/STDERR increment lands
                        # (父子统一 with takeover). Non-blocking — never stalls the
                        # abort poll below.
                        while not _partials.empty():
                            yield _partials.get_nowait()
                        if merged_abort.is_set():
                            _aborted_round = True
                            break
                        if _next_task is None:
                            _next_task = asyncio.ensure_future(
                                _agen.__anext__()
                            )
                        done, _pending = await asyncio.wait(
                            {_next_task}, timeout=_ABORT_POLL_S
                        )
                        if _next_task not in done:
                            # Poll window elapsed, slot still running — loop to
                            # re-drain partials + re-check the abort flag (keep
                            # the SAME future).
                            continue
                        try:
                            item = _next_task.result()
                        except StopAsyncIteration:
                            # Stream exhausted — every slot yielded. Flush any
                            # partials produced just before the last final.
                            _next_task = None
                            while not _partials.empty():
                                yield _partials.get_nowait()
                            break
                        _next_task = None
                        _seen_call_ids.add(item.call_id)
                        yield _shape_final(item)
                    if _aborted_round:
                        # Cooperative whole-round abort: the ``finally`` below
                        # reaps the parked ``__anext__`` future + ``aclose()``s
                        # the generator (its reap cancels + reaps pending slot
                        # tasks → exec.py tree-kill). Emit a bounded
                        # ``[interrupted]`` final for each un-yielded call FIRST
                        # so the round completes (kernel pairs by call_id)
                        # instead of leaving cards stuck "executing".
                        for _name, _args, _cid in tool_metas:
                            if _cid in _seen_call_ids:
                                continue
                            yield ToolExecutionItem(
                                call_id=_cid,
                                tool_name=_name,
                                arguments=_args,
                                partial=False,
                                result_text=(
                                    "[interrupted] tool call aborted by user"
                                ),
                                ok=False,
                            )
                        return
                finally:
                    # Failsafe teardown for the OUTER-cancel / GeneratorExit /
                    # cooperative-abort path. Reap the parked ``__anext__``
                    # future FIRST (cancel + await) — you cannot ``aclose()`` an
                    # async generator while one of its ``__anext__`` coroutines
                    # is still running ("asynchronous generator is already
                    # running"). Then ``aclose()`` the stream so its own reap
                    # cancels + reaps every still-running slot task (exec.py
                    # tree-kill) → no orphaned subprocess. Idempotent — closing
                    # an already-exhausted / already-closed generator is a no-op.
                    # NEVER swallow the outer cancel: we only await the stream's
                    # own reap, then let the original exception propagate.
                    if _next_task is not None and not _next_task.done():
                        _next_task.cancel()
                        try:
                            await _next_task
                        except (asyncio.CancelledError, Exception):  # noqa: BLE001
                            pass
                    await _agen.aclose()

            return _gen()

        def _abort_check() -> bool:
            # Fix 2: the kernel's between-rounds + per-frame cooperative abort
            # fires when EITHER this sub-agent's own registry event is set
            # (standalone ⏹ / manual interrupt / Fix-1 owner cascade) OR the
            # PARENT tab's Stop handle is set. State-Truth-First: returns True
            # ONLY when a real abort source is set — never a false positive.
            return merged_abort.is_set()

        # Sub-agent model context window (same resolver the main agent uses —
        # ``streaming.py:6090``). ``local::`` prefix stripped so an on-device
        # model resolves its own window; unknown ids fall back to the resolver's
        # conservative default. Computed once per run (the model is fixed for a
        # run). Feeds both the compaction trigger gate AND the live token badge
        # denominator the front-end shows.
        #
        # State-Truth-First (铁律 1 / 铁律 4): the budget denominator真值源 is the
        # sub-agent's OWN persisted ``session.model_id`` (migration 046) — the
        # user may have switched THIS sub-agent's model independently of the
        # parent. Fall back to the run's inherited ``model_hint`` only when the
        # session carries no model (no repo wired / legacy row) so existing
        # behaviour is preserved (no regression).
        _budget_model_raw = (
            session.model_id
            if session is not None and session.model_id is not None
            else model_hint
        )
        _ctx_model_id = (_budget_model_raw or "").removeprefix(
            _LOCAL_MODEL_HINT_PREFIX
        ) or "__unknown__"
        _context_limit = get_context_limit(_ctx_model_id)

        def _eff_prompt_from(usage: dict[str, Any] | None) -> int | None:
            # Provider-corrected effective wire size (实发): Claude/Anthropic
            # report only the NON-cached portion in ``prompt_tokens`` and put the
            # cached remainder in ``cache_read_tokens`` — add it back ONLY for
            # Claude (OpenAI / Azure / Gemini / Vertex already fold cache into
            # ``prompt_tokens``). Shared口径 via ``effective_prompt_tokens`` —
            # NO cache_write fallback here (point ① keeps the raw口径; the
            # cache_write add-back belongs only to the accounting path ②③). The
            # helper returns 0 for no-usage / non-positive; restore the legacy
            # ``None`` sentinel this call site has always returned (byte-for-byte
            # equivalent — the sole consumer ``_compact_hook`` passes it as
            # ``measured_eff_prompt``, which the engine guards with
            # ``is not None and > 0``, so 0 and None are interchangeable there).
            _eff = _effective_prompt_tokens(
                usage,
                is_anthropic=_is_anthropic_family(model_hint),
                include_cache_write_fallback=False,
            )
            return _eff or None

        def _round_real_prompt(
            usage: dict[str, Any] | None, prev_real: int
        ) -> tuple[int, int, str]:
            # Cumulative provider-measured real prompt tokens for ONE round
            # (State-Truth-First 铁律 1) — the size of the wire THIS round
            # received as input. Mirrors the CLOUD branch of the main agent's
            # ``streaming._extract_round_real_prompt``: prefer the provider's
            # ``prompt_tokens`` (already ``_extract_usage``-corrected upstream),
            # belt-and-braces re-derive from ``total - completion`` when larger.
            # The sub-agent does NOT carry the main agent's tiktoken fallback
            # (it would need ``_precise_text_tokens``, which lives in
            # ``streaming.py`` — un-importable here without a cycle); a no-usage
            # (local / mock) round returns 0 / ``"unknown"`` so the engine
            # gracefully attributes that one group by char × density (per-group,
            # not whole wire) — no regression (the sub-agent had NO differential
            # attribution at all before this).
            if isinstance(usage, dict):
                try:
                    _pt = int(usage.get("prompt_tokens") or 0)
                    _ct = int(usage.get("completion_tokens") or 0)
                    _tt = int(usage.get("total_tokens") or 0)
                except (TypeError, ValueError):
                    _pt = _ct = _tt = 0
                if _tt > 0 and _ct >= 0 and (_tt - _ct) > _pt:
                    _pt = _tt - _ct
                if _pt > 0:
                    return _pt, _ct, "cloud"
            return 0, 0, "unknown"

        async def _compact_hook(
            round_no: int, wire: list[dict[str, Any]]
        ) -> list[dict[str, Any]] | None:
            # Inter-round context management via the SHARED
            # :class:`CompactionCheckpointEngine` — BYTE-FOR-BYTE the algorithm
            # the main agent runs (differential real-token attribution +
            # checkpoint). Returns a rebuilt wire to REPLACE the growing wire
            # when compaction fired (the kernel swaps it in), or ``None`` to
            # leave the wire unchanged.
            #
            # Thread the MOST-RECENT round's provider-measured ``实发`` into the
            # trigger gate (mid-turn this usage is NOT yet on any persisted
            # message — the size actually being sent THIS turn is the correct
            # compaction judgement; State-Truth-First). The engine no-ops (and
            # we fall through to ``None`` → kernel's built-in compression) when
            # no compressor is wired (legacy/stub parity).
            nonlocal _sub_compaction_fired
            _measured_eff = _eff_prompt_from(_last_round_usage["usage"])
            ckpt = await self._compaction_engine.maybe_compress(
                checkpoint_key=(
                    session.id.value if session is not None
                    else str(conversation_id)
                ),
                assembled_wire=wire,
                # The sub-agent has NO persisted ``Conversation`` history past
                # an anchor (its whole world IS the in-flight ``wire``), so the
                # cross-anchor history slice is empty and the anchor is 0 — the
                # ``completed_rounds`` differential carries ALL the real-token
                # attribution (live-wire mode).
                history_messages_since_anchor=[],
                anchor_index=0,
                anchor_message_id=None,
                completed_rounds=completed_rounds,
                model_hint=model_hint or "",
                context_limit=_context_limit,
                measured_eff_prompt=_measured_eff,
                presend_eff_fallback=0,
                live_wire_mode=True,
                persist_id=None,  # pure in-memory checkpoint (ephemeral run)
            )
            if ckpt is None:
                return None
            # The checkpoint's ``compacted_wire`` already folds EVERY prior
            # in-flight round — clear ``completed_rounds`` so the engine does not
            # double-count them on the NEXT compaction (the same duplication trap
            # the main loop guards at ``streaming.py:7010``).
            completed_rounds.clear()
            # P1 #9: signal the NEXT _open_round_stream (same round, runs after
            # this hook) that compaction fired → it re-bases the cache anchor.
            # Direct truth (compaction ran) rather than inferring from wire length.
            _sub_compaction_fired = True
            return [dict(m) for m in ckpt.compacted_wire]

        async def _on_tool_round_complete(round_num: int) -> None:
            # Record this TOOL round into ``completed_rounds`` for the
            # differential compaction engine. Fires AFTER the kernel grew the
            # round's wire (``assistant{tool_calls}`` + ``role:tool`` replies),
            # so we recover the round's ``tool_results`` from the wire tail past
            # the start index ``_on_round_end`` stashed. State-Truth-First: the
            # round's ``real_prompt_tokens`` come from the provider usage that
            # round measured (cloud path). MUST run before ``_record_round`` so a
            # later compaction this run sees the just-completed round.
            _pend = _pending_round.pop(round_num, None)
            if _pend is not None:
                _start = int(_pend.get("wire_start") or 0)
                _round_results: list[dict[str, Any]] = []
                for _m in wire_messages[_start:]:
                    if isinstance(_m, dict) and _m.get("role") == "tool":
                        _round_results.append(
                            {"output": _m.get("content")}
                        )
                _prev_real = (
                    completed_rounds[-1].real_prompt_tokens
                    if completed_rounds
                    else 0
                )
                _real, _comp, _src = _round_real_prompt(
                    _pend.get("usage"), _prev_real
                )
                completed_rounds.append(
                    _SubAgentCompletedRound(
                        text=str(_pend.get("text") or ""),
                        tool_calls=list(_pend.get("tool_calls") or []),
                        tool_results=_round_results,
                        real_prompt_tokens=_real,
                        completion_tokens=_comp,
                        source=_src,
                    )
                )
            # Persist the running wire history after each completed tool round
            # (best-effort) so a crash / interrupt mid-run still leaves a
            # resumable snapshot. The kernel fires this AFTER the round's
            # ``role:tool`` replies are appended and BEFORE the next round's
            # abort check (State-Truth-First: an abort flag a save sets is
            # observed by the next round). ``rounds`` is the CUMULATIVE total
            # (block 4): prior runs + this run's round_num. This save also
            # flushes the in-memory per-round snapshots + cumulative usage the
            # hooks below recorded (they ride THIS save's CAS version chain —
            # no separate save, no concurrent-write risk).
            #
            # 方案A (nested/grand ↑↓ fix): stamp the per-round ``usage`` +
            # ``request_id`` onto the just-completed assistant wire turns BEFORE
            # this mid-run persist — the SAME SHARED helper the autonomous
            # finalize tail uses. Without it, a run that is later aclose'd /
            # interrupted (a grand sub-agent whose parent turn was cancelled)
            # would persist THIS ``_record_round`` snapshot with NO per-message
            # usage → the front-end hid ↑↓. Idempotent: turns already stamped are
            # left untouched, so the terminal finalize re-stamp is a no-op on
            # them. DISPLAY-ONLY — never touches counters / billing.
            _stamp_wire_turn_usage(
                wire_messages, _seed_len, _round_usages, _round_request_ids,
            )
            await self._record_round(
                session, wire_messages, base_rounds + round_num,
                thought_signatures=_thought_signatures,
            )

        async def _on_round_open(
            round_no: int, send_wire: list[dict[str, Any]]
        ) -> None:
            # Per-round prompt snapshot (回看 parity with the main agent): save
            # the EXACT wire SENT this round (system + accumulating history +
            # per-round assistant{tool_calls}/tool blocks) into the SHARED
            # ``PromptSnapshotStorePort`` — the SAME store + SAME shared-prefix
            # path the main agent's ``_save_round_snapshot`` uses — mint a
            # per-round ``request_id``, and remember it (stamped onto the
            # persisted assistant turn after the loop). No-op when the store is
            # unwired (legacy/stub parity). Best-effort: a failure never blocks
            # the run.
            if self._prompt_snapshot_store is None:
                return
            rid = await self._save_sub_round_snapshot(
                send_wire=send_wire,
                turn_ref=_snapshot_turn_ref,
                model_hint=model_hint,
                request_options=_snapshot_request_options,
            )
            if rid is not None:
                _round_request_ids[round_no] = rid

        # P10 — per-run state for the empty-completion retry and no-progress
        # circuit-breaker (parity with the main agent's
        # ``StreamChatUseCase._run_followup_loop`` body). Previously the
        # sub-agent loop did NOT adopt these guards (the old comment said
        # so explicitly); on a Claude bridge that returns an empty CHUNK
        # after a tool round the sub-agent silently terminated, and on a
        # model that re-emitted the same tool call in a tight loop the
        # sub-agent burned through ``max_rounds`` doing the same work.
        # Both failure modes are user-observable, so we ROLL THIS BACK
        # to "adopt the main agent's guard semantics" (AGENTS.md
        # 🟡🟡 "发现缺陷必须修，绝不将错就错").
        _SUB_NO_PROGRESS_LIMIT: int = 3
        _last_call_sig_holder: dict[str, str | None] = {"sig": None}
        _repeat_count_holder: dict[str, int] = {"count": 0}
        _last_tool_round_holder: dict[str, int] = {"round": 0}
        _empty_completion_retry_used = {"used": False}
        _no_progress_break = {"triggered": False}
        # Per-conversation token-budget (max_budget_tokens) sub-agent state.
        # ``triggered`` flips once a completed sub-agent round pushes the PARENT
        # conversation's shared pool at/over the cap; the loop then stops before
        # opening another round (same allow-overshoot-then-stop rule as the main
        # agent). ``local_hinted`` throttles the "not enforceable on local"
        # advisory to once per sub-agent run.
        _budget_break = {"triggered": False, "local_hinted": False}

        def _sub_tool_calls_signature(
            tool_calls: list[dict[str, Any]],
        ) -> str:
            """Stable signature of a round's tool calls (name + args).

            Mirrors ``streaming._tool_calls_signature`` but operates on
            the raw payload dicts the sub-agent kernel hands to
            ``_on_round_end`` (the main agent gets ``StreamFrame``
            objects; the dict form here carries the same ``tool_name`` /
            ``arguments`` keys). Order-independent.
            """
            parts: list[str] = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                name = tc.get("tool_name") or ""
                args = tc.get("arguments")
                if args is None:
                    args = tc.get("args")
                try:
                    args_str = json.dumps(
                        args if isinstance(args, dict) else {},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                except (TypeError, ValueError):
                    args_str = "{}"
                parts.append(f"{name}|{args_str}")
            if not parts:
                return ""
            return "\n".join(sorted(parts))

        async def _on_round_end(
            round_no: int,
            end_payload: dict[str, Any],
            round_text: str,
            tool_calls: list[dict[str, Any]],
        ) -> "RoundEndDecision":
            # Per-round token usage (回看 parity): remember this round's provider
            # usage dict so it can be stamped onto the round's persisted
            # assistant turn (per-message token line, IDENTICAL to the main
            # agent). Also keep folding into the session's CUMULATIVE
            # ``usage`` field (unchanged) for any aggregate consumer. P10:
            # the sub-agent NOW adopts the main loop's empty-completion
            # retry + no-progress circuit-breaker too (the older comment
            # "does NOT adopt these options" is订正 — the gap caused
            # user-visible regressions on Claude bridges and tight repeat
            # loops; see the tail of this function for the retry / stop
            # decision logic).
            _usage = end_payload.get("usage")
            if isinstance(_usage, dict):
                _round_usages[round_no] = dict(_usage)
                # 方案B: learn this gateway's prompt-cache support from the
                # round's raw usage signal. ``_extract_usage`` emits
                # ``provider_reported_cache = True`` ONLY when the gateway echoed
                # a cache field (majority no-cache path omits the key). Since it
                # ALWAYS computes the signal on the real path, an absent key on a
                # real usage dict means "gateway did NOT report cache" → mark
                # no-cache (enables next-round aging); ``True`` → supports cache →
                # keep aging OFF. Derived from RAW BEFORE the corrected branch
                # zeros cache_read_tokens (the坑). Written into the SAME shared
                # registry the main agent + routing stream use.
                if self._provider_cache_registry is not None:
                    self._provider_cache_registry.mark(
                        model_hint,
                        bool(_usage.get("provider_reported_cache")),
                    )
            # Full-unification (Step 3): capture Vertex AI thought_signatures
            # from the raw TOOL_CALL frame payloads (``tool_calls`` here is the
            # list of frame.payload dicts, which carry ``thought_signature`` when
            # the model emitted one). Key by the SAME call_id the wire uses —
            # the model's own id when present, else the synthetic
            # ``sub_{round}_{idx}`` id ``_build_tool_metas`` assigns (built in the
            # SAME order), so the signature maps to the right wire entry. Stored
            # so ``_wire_to_structured_messages`` folds them onto the structured
            # card at finalize time — making the transcript signature-lossless.
            for _idx, tc in enumerate(tool_calls):
                if not isinstance(tc, dict):
                    continue
                sig = tc.get("thought_signature")
                if not sig:
                    continue
                cid = tc.get("tool_call_id") or f"sub_{round_no}_{_idx}"
                _thought_signatures[cid] = sig
            # Differential-compaction seam: remember the MOST-RECENT round's
            # usage (threaded into ``_compact_hook``'s trigger gate as the
            # measured ``实发``) and stash this round's text + raw tool_calls +
            # usage + the wire length BEFORE its blocks are appended. The kernel
            # appends this round's ``assistant{tool_calls}`` + ``role:tool``
            # replies AFTER us (it fires ``on_round_end`` before growing the
            # wire), so ``len(wire_messages)`` here is exactly the round's start
            # index — ``_on_tool_round_complete`` slices ``wire_messages`` from
            # it to recover this round's ``role:tool`` replies as ``tool_results``
            # and build the ``_SubAgentCompletedRound``.
            _last_round_usage["usage"] = (
                dict(_usage) if isinstance(_usage, dict) else None
            )
            # ── Per-conversation token-budget (max_budget_tokens) ─────────────
            # Fold THIS round's PROVIDER-MEASURED net-new tokens into the PARENT
            # conversation's SHARED pool (user's mental model: the whole chat —
            # main agent + all sub-agents — shares one N-token budget). Keyed by
            # the parent ``conversation_id`` (a sub-agent's ``conversation_id`` IS
            # its parent conversation). Same rules as the main agent: local turns
            # skip enforcement (no authoritative usage), a prompt=0+completion=0
            # round is not enforced (Gemini partial reliability), and once the
            # cap is met we stop AFTER the in-flight round (allow-overshoot). The
            # abort is realised as ``RoundEndDecision(stop=True)`` below (checked
            # in the has_pending branch) so the sub-agent closes cleanly with a
            # budget notice appended to its result.
            if _is_local_model_hint(model_hint):
                if not _budget_break["local_hinted"]:
                    _budget_break["local_hinted"] = True
                    _log.debug(
                        "chat.subagent.budget_local_skip",
                        conversation_id=conversation_id.value,
                        model_hint=model_hint,
                    )
            else:
                _br = await self._budget_tracker.check(conversation_id)
                if _br.enabled:
                    _lru_u = (
                        _usage if isinstance(_usage, dict) else None
                    )
                    _raw_pt = int(_lru_u.get("prompt_tokens") or 0) if _lru_u else 0
                    _comp_t = (
                        int(_lru_u.get("completion_tokens") or 0) if _lru_u else 0
                    )
                    if _lru_u is not None and _raw_pt == 0 and _comp_t == 0:
                        _log.warning(
                            "chat.subagent.budget_round_no_usage",
                            round_no=round_no,
                            conversation_id=conversation_id.value,
                            model_hint=model_hint,
                        )
                    else:
                        _eff_pt = _effective_prompt_tokens(
                            _lru_u,
                            is_anthropic=_is_anthropic_family(model_hint),
                            include_cache_write_fallback=True,
                        )
                        _delta_b = _eff_pt + _comp_t
                        if _delta_b > 0:
                            await self._budget_tracker.observe(
                                conversation_id, _delta_b
                            )
                            _br = await self._budget_tracker.check(
                                conversation_id
                            )
                    if _br.exceeded:
                        _budget_break["triggered"] = True
                        _log.info(
                            "chat.subagent.budget_exceeded",
                            round_no=round_no,
                            conversation_id=conversation_id.value,
                            used_tokens=_br.used,
                            max_tokens=_br.max_tokens,
                        )
            if tool_calls:
                _pending_round[round_no] = {
                    "text": round_text,
                    "tool_calls": list(tool_calls),
                    "usage": dict(_usage) if isinstance(_usage, dict) else None,
                    "wire_start": len(wire_messages),
                }
            if session is not None:
                # Provider-corrected effective wire size for the replace-last
                # ``last_prompt_tokens`` figure (the standalone sub-agent tab's
                # context badge), recorded via the SHARED
                # ``record_subagent_turn_usage`` helper (single source of truth
                # with the take-over path ③). Claude/Anthropic report only the
                # NON-cached portion in ``prompt_tokens`` and put the cached
                # remainder in ``cache_read_tokens`` (falling back to
                # ``prompt_tokens_details.cache_write_tokens`` for a prompt-cache-
                # WRITE round) — so a cache-hit final round would otherwise store
                # a tiny ``prompt_tokens`` (e.g. 2) instead of the true wire size
                # (e.g. 6372) and the badge reads "~0.0K". The helper injects the
                # corrected figure as ``last_round_prompt_tokens`` (which
                # ``accumulate_usage`` PREFERS) only when it exceeds the raw
                # ``prompt_tokens``; the cumulative ``usage`` sum keeps folding
                # the raw per-key values unchanged, and a no-usage round is a
                # no-op (prior value preserved).
                _record_subagent_turn_usage(
                    session,
                    _usage,
                    model_id=model_hint,
                    now=self._clock.now(),
                )
                # Recompute the eff figure for the DIAG log only (the helper does
                # not return it). Same口径 (cache_write fallback) the helper used;
                # mirrors the field ``accumulate_usage`` consumed —
                # ``last_round_prompt_tokens`` iff eff > raw prompt_tokens.
                _diag_pt = (
                    int(_usage.get("prompt_tokens") or 0)
                    if isinstance(_usage, dict)
                    else 0
                )
                _diag_eff = _effective_prompt_tokens(
                    _usage,
                    is_anthropic=_is_anthropic_family(model_hint),
                    include_cache_write_fallback=True,
                )
                # DIAG (sub-agent token-badge investigation): the sub-agent usage
                # chain was previously SILENT (no log between the kernel round and
                # the persisted ``last_prompt_tokens``), so a "badge ~0.0K" could
                # not be root-caused from logs. Surface the raw provider prompt,
                # the eff figure we fed ``accumulate_usage``, and the resulting
                # domain ``last_prompt_tokens`` so the next run pins the truth
                # (and DISTINGUISHES sub-agent rounds from the parent's, which
                # share the parent ``conversation_id`` in ``extract_usage`` logs).
                _log.info(
                    "chat.diag.subagent_usage",
                    subagent_id=session.id.value,
                    round_no=round_no,
                    raw_prompt_tokens=(
                        _usage.get("prompt_tokens")
                        if isinstance(_usage, dict)
                        else None
                    ),
                    eff_for_accum=(
                        _diag_eff if _diag_eff > _diag_pt else None
                    ),
                    session_last_prompt_tokens=session.last_prompt_tokens,
                    model_hint=model_hint,
                )
                # Task B (State-Truth-First): publish the REAL running wire size
                # the session just recorded (``last_prompt_tokens`` — provider-
                # measured, replace-last semantics) so the round's stream events
                # carry a live, accurate used-token figure. Only when the domain
                # actually updated it (a round with usage); a no-usage round
                # leaves it at the prior value (never regresses to a stale
                # estimate).
                if session.last_prompt_tokens is not None:
                    _live_used_tokens["used"] = int(session.last_prompt_tokens)

            # P10 — empty-completion retry + no-progress circuit-breaker
            # (parity with the main agent's ``_on_round_end``,
            # streaming.py:7898-7991). Without these the sub-agent would
            # silently terminate on a Claude-bridge empty-after-tool round
            # and burn through ``max_rounds`` on a tight repeat loop.
            has_pending = bool(tool_calls)
            text_clean = round_text.strip()
            reason = end_payload.get("reason")
            is_stop = reason in (None, "completed", "stop")
            last_tool_round = _last_tool_round_holder["round"]
            # ── empty-completion retry (one-shot per run) ─────────────────
            # Mirrors V1 chat_handler.py:752-769: a STOP / "" finish_reason
            # after a tool round that produced NO visible text is a known
            # Gemini / Claude-bridge quirk. We synthesise a user nudge and
            # let the kernel re-open the round with the SAME wire +
            # appended ``user: 请基于工具结果总结回答。`` so the model gets
            # a clear signal to summarise the tool output rather than
            # leaving the run dangling.
            if (
                is_stop
                and not text_clean
                and not has_pending
                and last_tool_round > 0
                and not _empty_completion_retry_used["used"]
                and round_no > 1
            ):
                _log.info(
                    "chat.subagent.empty_completion_retry",
                    round_no=round_no,
                    last_tool_round=last_tool_round,
                )
                _empty_completion_retry_used["used"] = True
                synthetic_wire = list(wire_messages)
                synthetic_wire.append(
                    {
                        "role": "user",
                        "content": "请基于工具结果总结回答。",
                    },
                )
                return RoundEndDecision(
                    retry=True, retry_wire=synthetic_wire,
                )
            # ── no-progress circuit breaker ─────────────────────────────
            # When the model issues the SAME tool_calls signature N times
            # in a row, it is stuck — terminate cleanly instead of letting
            # the run hit ``max_rounds`` doing the same thing. Matches the
            # main agent's ``_NO_PROGRESS_LIMIT=3`` and the same diagnostic
            # text style. State-Truth-First: only signatures from rounds
            # that actually ran tools (``tool_calls`` non-empty) reset /
            # bump the counter; a no-tool round is irrelevant to "stuck on
            # the same tool batch".
            # ── Per-conversation token-budget cap reached (max_budget_tokens) ─
            # The parent conversation's shared pool hit the cap on this round
            # (folded above). Do NOT open another sub-agent round — stop AFTER
            # this (already-sent) round with a budget notice appended to the
            # sub-agent's result (same shape as the no-progress break). Only
            # relevant when the round issued tool calls (a terminal round ends
            # the run anyway). Same allow-overshoot-then-stop rule as the main
            # agent.
            if _budget_break["triggered"] and has_pending:
                return RoundEndDecision(
                    stop=True,
                    final_text=(
                        "\n\n_(已达到本会话的 token 预算上限，"
                        "子任务已在本轮结束后停止。)_"
                    ),
                )
            if has_pending:
                _last_tool_round_holder["round"] = round_no
                _sig = _sub_tool_calls_signature(tool_calls)
                if _sig != "" and _sig == _last_call_sig_holder["sig"]:
                    _repeat_count_holder["count"] += 1
                else:
                    _repeat_count_holder["count"] = 1
                    _last_call_sig_holder["sig"] = _sig
                if _repeat_count_holder["count"] >= _SUB_NO_PROGRESS_LIMIT:
                    _log.warning(
                        "chat.subagent.no_progress_break",
                        round_no=round_no,
                        repeats=_repeat_count_holder["count"],
                    )
                    _no_progress_break["triggered"] = True
                    # Stop with a short final-text suffix so the parent
                    # agent's result line carries the diagnostic — same
                    # shape the main agent uses.
                    return RoundEndDecision(
                        stop=True,
                        final_text=(
                            "\n\n_(检测到工具调用在原地重复、未取得进展，"
                            "已自动结束本轮。可能是上下文不完整导致；请重述"
                            "需求或换个说法让我重试。)_"
                        ),
                    )
            return RoundEndDecision()

        # Drive the kernel and adapt neutral events → ``subagent_*`` dicts.
        round_text = ""

        async for ev in self._kernel.run(
            wire_messages=wire_messages,
            open_round_stream=_open_round_stream,
            tool_executor=_tool_executor,
            build_tool_metas=_build_tool_metas,
            max_rounds=effective_max_rounds,
            abort_check=_abort_check,
            model_hint=model_hint,
            include_tool_name_in_reply=True,
            assistant_timestamp=lambda: self._clock.now().isoformat(),
            compress_log_context={"agent_index": agent_index},
            on_tool_round_complete=_on_tool_round_complete,
            on_round_open=_on_round_open,
            on_round_end=_on_round_end,
            compact_hook=_compact_hook,
        ):
            if isinstance(ev, KernelRoundStarted):
                rounds_done = ev.round_no
            elif isinstance(ev, KernelChunk):
                rounds_done = ev.round_no
                yield {
                    "type": "subagent_output",
                    "index": agent_index,
                    "content": ev.text,
                    "round": ev.round_no,
                }
            elif isinstance(ev, KernelError):
                rounds_done = ev.round_no
                accumulated_text.append(
                    f"[sub-agent round {ev.round_no} error: {ev.message}]"
                )
                yield {
                    "type": "subagent_error",
                    "index": agent_index,
                    "message": ev.message,
                    "round": ev.round_no,
                }
                break
            elif isinstance(ev, KernelToolCallsIssued):
                rounds_done = ev.round_no
                # Step 1 — emit ``subagent_tool`` for EVERY call BEFORE any
                # execution starts (chat_handler.py:2298-2304) so the UI can
                # show "🔧 tool_name(args)" for all in-flight calls up front.
                for tool_name, arguments, _call_id in ev.tool_metas:
                    yield {
                        "type": "subagent_tool",
                        "index": agent_index,
                        "tool_name": tool_name,
                        "tool_args": arguments,
                        "tool_call_id": _call_id,
                        "round": ev.round_no,
                    }
            elif isinstance(ev, KernelToolResult):
                # Step 4 — emit one ``subagent_tool_result`` per call so the UI
                # renders a structured, collapsible result panel under the
                # matching ``subagent_tool`` row. The result text is the SAME
                # truncated string fed back to the LLM.
                yield {
                    "type": "subagent_tool_result",
                    "index": agent_index,
                    "tool_name": ev.tool_name,
                    "tool_call_id": ev.call_id,
                    "result": ev.result_text,
                    "ok": ev.ok,
                    "duration_ms": ev.duration_ms,
                    "round": ev.round_no,
                    # Single-tool cancel marker (tail-appended, §3.1): the
                    # spectator/takeover UI renders the card "已取消/cancelled"
                    # (父子统一 with the takeover path's ``tool_result`` frame's
                    # ``cancelled`` field). Absent-as-False for every non-cancel
                    # result keeps old emitters byte-for-byte unchanged.
                    **({"cancelled": True} if ev.cancelled else {}),
                    # Task B — live, provider-measured running context size +
                    # the model's window (tail-appended optional fields, §3.1).
                    # ``_on_round_end`` (which ran for THIS round before the tool
                    # results) already refreshed ``_live_used_tokens`` from the
                    # session's ``last_prompt_tokens``, so this carries the REAL
                    # wire size as of this round. Lets the standalone sub-agent
                    # tab's context badge update live every round instead of
                    # freezing at round 1 until the run finishes. Omitted when no
                    # usage has landed yet (local / stub — field simply absent).
                    **(
                        {
                            "used_tokens": _live_used_tokens["used"],
                            "context_limit": _context_limit,
                        }
                        if _live_used_tokens["used"] is not None
                        else {}
                    ),
                }
            elif isinstance(ev, KernelToolPartial):
                # Live PARTIAL result fragment from a streaming tool (e.g. the
                # sub-agent's own exec stdout/stderr increments — L3). Forward
                # verbatim so the spectator / takeover UI streams the tool card's
                # output live (父子统一 with the takeover path, which renders
                # ``tool_result{partial:true}`` frames). The kernel never folds a
                # partial into the wire, so this is purely a live-UI enhancement.
                # A partial that carries ONLY a passthrough ``frame`` (an
                # ``agent`` dispatch's SUBAGENT_* / AGENT_SUMMARY frame) has an
                # empty ``delta`` and is routed to the frame passthrough below.
                if getattr(ev, "frame", None) is not None:
                    async for _sub in self._forward_passthrough_frame(
                        ev.frame, agent_index=agent_index, round_no=ev.round_no
                    ):
                        yield _sub
                else:
                    yield {
                        "type": "subagent_tool_partial",
                        "index": agent_index,
                        "tool_name": ev.tool_name,
                        "tool_call_id": ev.call_id,
                        "delta": ev.delta,
                        "round": ev.round_no,
                    }
            elif isinstance(ev, KernelStreamPassthrough):
                # A non-control LLM-stream frame (REASONING "thinking" tokens /
                # cloud ``generating_args`` progress / NETWORK_RETRY banner) the
                # kernel forwards verbatim. The takeover main loop stamps + emits
                # these to the live UI; the sub-agent loop historically DROPPED
                # them (no branch → silently skipped), so a spectator saw no
                # reasoning / progress. Forward them as ``subagent_*`` events so
                # the spectator view matches takeover (增强, never a regression —
                # the frames were never persisted, only live UI).
                async for _sub in self._forward_passthrough_frame(
                    ev.frame, agent_index=agent_index, round_no=ev.round_no
                ):
                    yield _sub
            elif isinstance(ev, KernelFinished):
                rounds_done = ev.round_no
                round_text = ev.final_text
                # The kernel already appended the final assistant turn to
                # ``wire_messages`` (so a later wake sees the model's own
                # conclusion).
                accumulated_text.append(round_text)
                break
            elif isinstance(ev, KernelMaxRoundsReached):
                rounds_done = ev.round_no
                round_text = ev.last_text
                # Explicit "did NOT finish" marker (Fix: V1/v0.5 returned a
                # half-result with no clear signal). The ``[sub-agent
                # INCOMPLETE ...]`` prefix is human- and model-readable so the
                # parent can re-dispatch / narrow the task.
                accumulated_text.append(
                    f"[sub-agent INCOMPLETE — reached the maximum of "
                    f"{effective_max_rounds} rounds without finishing; the result "
                    f"below is PARTIAL] Last response: {round_text}"
                )
                break
            elif isinstance(ev, KernelAborted):
                # Cooperative interrupt fired before a round opened (V1 parity:
                # the in-flight round, if any, already ran to completion).
                interrupted = True
                rounds_done = ev.round_no - 1
                accumulated_text.append(
                    "[sub-agent INTERRUPTED — stopped by user before "
                    f"round {ev.round_no}]"
                )
                break

        # The parent agent only needs the
        # sub-agent's FINAL summary, not every round's text concatenated. We
        # return the LAST non-empty text fragment — which, given
        # ``_SUB_AGENT_SYSTEM_PROMPT``'s "FINAL REPLY FORMAT" directive, is the
        # structured summary. This is behaviour-preserving here because ONLY
        # terminal frames append to ``accumulated_text`` (KernelFinished →
        # ``round_text``; KernelError → ``[sub-agent round N error: ...]``;
        # KernelMaxRoundsReached → ``[sub-agent INCOMPLETE ...] Last response:
        # ...``; KernelAborted → ``[sub-agent INTERRUPTED ...]``) and each
        # ``break``s immediately, so the error / interrupt / incomplete markers
        # are ALWAYS the last (and only) fragment when they fire — the
        # "did-not-finish" signal the parent relies on is therefore preserved
        # verbatim, never dropped by taking "only the last fragment".
        final = ""
        for part in reversed(accumulated_text):
            if part and part.strip():
                final = part.strip()
                break
        if not final:
            final = "[sub-agent produced no output]"

        # Safety net: even if the sub-agent ignores the FINAL REPLY FORMAT
        # directive and emits a huge final message, cap what enters the PARENT
        # context.
        _FINAL_HARD_CAP = 8_000
        if len(final) > _FINAL_HARD_CAP:
            omitted = len(final) - _FINAL_HARD_CAP
            # A sub-agent's FINAL REPLY is one-shot natural-language prose — it
            # has no ``offset`` re-read or on-disk recovery path, so we cannot
            # hand the parent a continuation coordinate. Be explicit that the
            # tail is unrecoverable and steer the parent toward the actionable
            # remedy: re-dispatch asking the sub-agent for a more concise FINAL
            # REPLY (or the specific missing piece) rather than the full dump.
            final = (
                final[:_FINAL_HARD_CAP]
                + f"\n... [sub-agent final output truncated at "
                f"{_FINAL_HARD_CAP} chars ({omitted} more character(s) "
                f"dropped and NOT recoverable). The sub-agent's reply was too "
                f"long; if you need the omitted tail, re-dispatch the sub-agent "
                f"asking for a more concise FINAL REPLY or only the specific "
                f"information you still need.]"
            )

        # ── Stamp per-round request_id + usage onto the persisted wire ────────
        # Autonomous-completion tail: stamp THIS run's newly-appended assistant
        # turns via the SHARED ``_stamp_wire_turn_usage`` helper (same logic the
        # mid-run ``_record_round`` path now uses so a run that ends without a
        # clean finalize — a nested/grand sub-agent aclose'd when its parent turn
        # was cancelled — still persists per-message ``usage``). Idempotent, so a
        # turn already stamped by an earlier ``_record_round`` is left untouched.
        _stamp_wire_turn_usage(
            wire_messages, _seed_len, _round_usages, _round_request_ids,
        )

        # Persist the final state (best-effort; no-op when no repo wired). An
        # error round marks the session ERROR; otherwise DONE. The final wire
        # snapshot is stored too so a woken session continues from the exact
        # end-of-run context.
        had_error = any(
            part.startswith("[sub-agent round ") and "error:" in part
            for part in accumulated_text
        )
        subagent_id = await self._finalize_session(
            session,
            wire_messages=wire_messages,
            rounds=base_rounds + rounds_done,
            had_error=had_error,
            interrupted=interrupted,
            thought_signatures=_thought_signatures,
        )

        # Block 3 — terminal frame. Yield BEFORE dropping the cancellation
        # registration so the registry record stays "findable" for the entire
        # window the client perceives the run as in-flight (until the
        # ``subagent_done`` frame lands). Otherwise there is a tiny but real
        # gap — registry record already deleted, ``done`` frame not yet on the
        # wire — during which a user-pressed ⏹ would POST
        # ``/api/chat/subagents/{id}/interrupt`` → ``registry.abort()`` misses
        # → returns ``aborted=False`` → the front-end's optimistic-rollback
        # snaps the UI back to ``streaming`` and the user sees "I pressed
        # Stop and nothing happened" right before the natural ``done`` lands.
        # Keeping the record alive across the yield closes that race: a late
        # interrupt either hits a still-alive record (and harmlessly fires its
        # already-fired event) OR arrives after the ``done`` frame settled the
        # UI to ``done`` — in BOTH cases the user sees a consistent terminal
        # state, never the "snap back" artifact. The unregister moves into the
        # paragraph below so it is the last thing this generator does.
        done_event: SubAgentEvent = {
            "type": "subagent_done",
            "index": agent_index,
            "result": final,
            "rounds": rounds_done,
        }
        # Task B — final live token figure on the terminal frame (tail-appended
        # optional fields, §3.1) so a tab that only catches ``subagent_done``
        # (backfill / late subscribe) still shows the run's last真实 wire size +
        # the model window. Omitted when no usage ever landed (local / stub).
        if _live_used_tokens["used"] is not None:
            done_event["used_tokens"] = _live_used_tokens["used"]
            done_event["context_limit"] = _context_limit
        # ``subagent_id`` is the resumable handle (optional appended field,
        # §3.1) — present only when the session was persisted. The main agent
        # re-passes it as ``resume_subagent_id`` to wake this sub-agent; the
        # user clicks it to open the sub-agent in a new tab.
        if subagent_id is not None:
            done_event["subagent_id"] = subagent_id
        yield done_event

        # Block 3 — drop this sub-agent's cancellation flag now the run is
        # over AND the terminal frame has been emitted (the flag is single-run;
        # a later wake registers a fresh one). MUST run after the yield so the
        # registry → ``done`` window described above never opens.
        if self._abort_registry is not None and abort_id is not None:
            self._abort_registry.unregister(abort_id)

    async def _resolve_session(
        self,
        *,
        resume_session_id: SubAgentSessionId | None,
        conversation_id: ConversationId,
        description: str,
        prompt_preview: str,
        subagent_type: str = GENERAL.name,
        title: str = "",
        allow_spawn: bool = False,
        model_hint: str | None = None,
        spawn_depth: int = 1,
        parent_subagent_id: SubAgentSessionId | None = None,
    ) -> tuple[SubAgentSession | None, list[dict[str, Any]]]:
        """Wake a prior sub-agent session or start a fresh one.

        Returns ``(session, prior_wire)``:

        * ``session`` — the in-memory working aggregate for this run, or
          ``None`` when no repo is wired (legacy/stub parity → no
          persistence). On a wake of a previously-terminal session a FRESH
          ``RUNNING`` working copy is built (same id, carrying the prior
          ``rounds`` / ``wire_messages``) so the aggregate's terminal-state
          invariants stay intact while still continuing the same logical
          thread.
        * ``prior_wire`` — the persisted wire history to seed this run with
          (empty for a brand-new session).

        Best-effort: any repo failure falls back to a fresh session so a
        sub-agent run is never blocked by a persistence hiccup.

        ``spawn_depth`` / ``parent_subagent_id`` (alpha unified spawn path):
        the tree edges recorded on a FRESH session. A wake path preserves the
        prior row's edges unchanged — the tree is an identity property of the
        node, not of a specific run.
        """
        repo = self._sub_agent_sessions
        if repo is None:
            return None, []
        now = self._clock.now()
        # Wake path -------------------------------------------------------
        if resume_session_id is not None:
            try:
                prior = await repo.find(resume_session_id)
            except Exception as exc:  # noqa: BLE001 — never block the run
                _log.warning(
                    "chat.agent_tool.resume.load_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                prior = None
            if prior is not None:
                # SUBAGENT-UNIFY-6: rebuild the feed-the-model wire from the
                # AUTHORITATIVE structured transcript (``prior.messages``) via
                # the SAME ``rebuild_history_wire_messages`` the main agent uses
                # for its cross-turn wire — the SOLE source (the legacy
                # ``wire_messages`` column is gone). thought_signature on the
                # structured tool-call cards is carried back onto the rebuilt
                # wire's ``assistant.tool_calls[i]`` (signature-lossless).
                prior_wire = _rebuild_history_wire_messages(
                    tuple(prior.messages)
                )
                # Build a fresh RUNNING working copy (same id) so a prior
                # terminal/awoken session can record new rounds without
                # tripping the aggregate's terminal-state guards.
                session = SubAgentSession(
                    id=prior.id,
                    root_conversation_id=prior.root_conversation_id,
                    parent_message_id=prior.parent_message_id,
                    # Tree edges are an identity property of the node — a
                    # wake never changes who my direct parent is or how deep
                    # I sit (migration 049).
                    parent_subagent_id=prior.parent_subagent_id,
                    depth=prior.depth,
                    subagent_type=prior.subagent_type,
                    title=prior.title,
                    prompt_preview=prior.prompt_preview,
                    status=SubAgentSessionStatus.RUNNING,
                    owner=prior.owner,
                    rounds=prior.rounds,
                    created_at=prior.created_at,
                    updated_at=now,
                    # Carry the loaded version so the working copy's first save
                    # does a correct compare-and-swap against the stored row
                    # (block 4 — concurrent resume + take-over consistency).
                    version=prior.version,
                    # Carry the prior cumulative usage + per-round snapshots so
                    # they accumulate ACROSS resumes (parity with ``rounds``;
                    # a woken sub-agent keeps its lifetime usage total + earlier
                    # rounds' prompt snapshots for回看).
                    usage=dict(prior.usage) if prior.usage is not None else None,
                    round_snapshots=(
                        {k: list(v) for k, v in prior.round_snapshots.items()}
                        if prior.round_snapshots is not None
                        else None
                    ),
                    # Carry the original spawn grant across a resume: the
                    # main agent's authorisation is a property of the
                    # sub-agent, not of any single run.
                    allow_spawn=prior.allow_spawn,
                    # Carry the persisted model (migration 046) so a resume
                    # keeps the sub-agent's OWN budget-denominator真值源 — the
                    # user's earlier per-sub-agent model switch survives a wake
                    # (State-Truth-First: session.model_id is authoritative, NOT
                    # the parent run's ``model_hint``). ``None`` on a never-set
                    # prior row → budget readers fall back as before.
                    model_id=prior.model_id,
                    model_provider=prior.model_provider,
                    # Carry the structured transcript so the woken run continues
                    # appending to it (the next ``record_messages`` replaces the
                    # whole list from the fresh wire, which was seeded from these
                    # same structured messages — so nothing is lost).
                    messages=list(prior.messages),
                )
                return session, prior_wire
            # Fall through to a fresh session when the id does not resolve
            # (invalid / deleted task_id → run as a new task, never error).
        # Fresh path ------------------------------------------------------
        session = SubAgentSession.start(
            session_id=SubAgentSessionId(self._ids.new_id()),
            root_conversation_id=conversation_id,
            now=now,
            # Alpha unified-spawn-path tree edges (migration 049): a depth-1
            # sub-agent has ``parent_subagent_id=None`` (direct parent is the
            # main agent, which lives outside this table); a grand/great-grand
            # sub-agent carries the id of its DIRECT parent sub-agent so the
            # tree is materialised in the DB, not lost inside a nested tool-
            # result string (the pre-α ``_spawn_grand_sub_agent`` branch's
            # architectural gap).
            parent_subagent_id=parent_subagent_id,
            depth=max(1, int(spawn_depth)),
            subagent_type=subagent_type,
            title=title,
            prompt_preview=(prompt_preview[:500] or description[:500]),
            allow_spawn=allow_spawn,
            # Default the sub-agent's OWN model to its PARENT's model (the
            # ``model_hint`` threaded from the spawning turn) — the拍板 design:
            # a sub-agent inherits its parent's model, persisted here as the
            # single budget-denominator真值源 (migration 046). Stored RAW (any
            # ``local::`` prefix preserved). No separate provider is threaded
            # into the sub-agent loop (only the model id via ``model_hint``), so
            # ``model_provider`` stays ``None`` until the user explicitly sets it
            # via PATCH /api/chat/subagents/{id}; budget readers then resolve by
            # id alone (legacy behaviour) — no regression.
            model_id=model_hint,
            model_provider=None,
        )
        return session, []

    async def _save_sub_round_snapshot(
        self,
        *,
        send_wire: list[dict[str, Any]],
        turn_ref: str,
        model_hint: str | None,
        request_options: dict[str, Any] | None,
    ) -> str | None:
        """Save ONE sub-agent round's prompt snapshot to the shared store.

        回看 parity with the main agent's
        ``StreamChatUseCase._save_round_snapshot``: each round's ``send_wire``
        is a strict PREFIX of the next round's (every round only appends
        ``assistant{tool_calls}`` + ``role:tool`` blocks), so we hand the store
        the full current list + a stable ``turn_ref`` and let it keep ONE shared
        list per turn (``save_shared_prefix``) — O(N) not O(N²). The blanked
        ``[tool_calls]`` sentinel + stripped ``created_at`` are already applied
        by the kernel's ``build_send_wire`` (so the stored snapshot is the EXACT
        bytes sent). Mints + returns this round's ``request_id`` (stamped onto
        the round's persisted assistant turn by the caller). Best-effort: any
        failure logs + returns ``None`` (the round still streams). ``None`` when
        no store is wired.
        """
        store = self._prompt_snapshot_store
        if store is None:
            return None
        try:
            messages_payload: list[dict[str, Any]] = [
                entry for entry in send_wire if isinstance(entry, dict)
            ]
            request_id = str(uuid.uuid4())
            await store.save_shared_prefix(
                request_id=request_id,
                turn_ref=turn_ref,
                shared_messages=messages_payload,
                prefix_len=len(messages_payload),
                model_id=model_hint or "",
                tool_mode="",
                timestamp=self._clock.now().isoformat(),
                request_options=request_options,
            )
            return request_id
        except Exception as exc:  # noqa: BLE001 — best-effort debug capture
            _log.warning(
                "chat.agent_tool.round_prompt_snapshot_save_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

    async def _record_round(
        self,
        session: SubAgentSession | None,
        wire_messages: list[dict[str, Any]],
        rounds: int,
        thought_signatures: dict[str, str] | None = None,
    ) -> None:
        """Persist the running round snapshot as the STRUCTURED transcript.

        SUBAGENT-UNIFY-6 (Step 3): the structured transcript
        (``session.messages``) is the SOLE persisted truth source — the legacy
        ``wire_messages`` column + ``record_round`` track are gone. The
        in-flight ``wire_messages`` snapshot is converted ONCE via the shared
        ``wire_to_structured_messages`` (carrying thought_signatures) and
        recorded via ``record_messages`` (which also advances ``rounds``). The
        feed-the-model wire for a later resume / take-over is rebuilt FROM this
        structured transcript via ``rebuild_history_wire_messages`` (口径 parity
        with the main agent). Best-effort: any failure logs but never blocks the
        run.
        """
        if session is None or self._sub_agent_sessions is None:
            return
        try:
            now = self._clock.now()
            structured = _wire_to_structured_messages(
                wire_messages, ids=self._ids, now=now,
                thought_signatures=thought_signatures,
            )
            session.record_messages(
                messages=structured, rounds=rounds, now=now,
            )
            await self._sub_agent_sessions.save(session)
            # Architectural alignment with main-agent broadcaster (root-cause
            # fix for the "sub-agent transcript rendered 2×/3×" duplication
            # bug 2026-07-01): the broadcaster buffer's semantic must be
            # "frames the persisted snapshot does NOT yet cover". Now that
            # `record_messages` has folded every published frame so far into
            # the authoritative transcript, trim them from the buffer — a
            # late subscriber (rail-chip click, restoreLayout re-hydrate,
            # take-over re-subscribe) will then receive only NEW frames that
            # the HTTP snapshot does not have, never an echo of frames the
            # snapshot already rendered. See
            # `SubAgentStreamBroadcaster.trim_all_published` docstring for
            # the full rationale + invariants.
            bc = self._stream_broadcaster
            if bc is not None:
                bc.trim_all_published(session.id.value)
        except Exception as exc:  # noqa: BLE001 — never block the run
            _log.warning(
                "chat.agent_tool.session.record_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                subagent_id=session.id.value,
                last_prompt_tokens=session.last_prompt_tokens,
            )

    async def _finalize_session(
        self,
        session: SubAgentSession | None,
        *,
        wire_messages: list[dict[str, Any]],
        rounds: int,
        had_error: bool,
        interrupted: bool = False,
        thought_signatures: dict[str, str] | None = None,
    ) -> str | None:
        """Persist the terminal sub-agent session; return its id (resumable).

        Returns ``None`` when no repo is wired (so the ``subagent_done`` frame
        omits ``subagent_id`` and stays byte-for-byte legacy-compatible).

        ``interrupted`` (block 3) settles the session as INTERRUPTED instead
        of DONE/ERROR when the user stopped it cooperatively via the interrupt
        endpoint — so the persisted status truthfully reflects the abort.
        """
        if session is None or self._sub_agent_sessions is None:
            return None
        now = self._clock.now()
        try:
            # SUBAGENT-UNIFY-6 (Step 3): persist the final round snapshot as the
            # STRUCTURED transcript (the SOLE truth source) BEFORE settling the
            # status (record_messages, like the old record_round, requires
            # non-terminal). The wire snapshot is converted once via the shared
            # converter; ``rounds`` advances here so a non-done terminal mark
            # (error / interrupted, which do NOT carry rounds) still records the
            # final counter. Gate ONLY on terminal status — a RUNNING working
            # copy must persist its final transcript regardless of ``owner``
            # (fix: 🟡-2 — a session re-woken after a user take-over carries
            # owner=USER; the USER_OWNED status itself is non-terminal so
            # record_messages permits it).
            if not session.is_terminal():
                try:
                    structured = _wire_to_structured_messages(
                        wire_messages, ids=self._ids, now=now,
                        thought_signatures=thought_signatures,
                    )
                    session.record_messages(
                        messages=structured, rounds=rounds, now=now,
                    )
                except Exception as exc_struct:  # noqa: BLE001
                    _log.warning(
                        "chat.agent_tool.session.structured_finalize_failed",
                        error=str(exc_struct),
                        error_type=type(exc_struct).__name__,
                        subagent_id=session.id.value,
                    )
                    # Conversion failed (extremely rare — the loop's own wire):
                    # still advance the round counter so a non-done terminal
                    # mark (error / interrupted, which do NOT carry rounds) does
                    # not stall the counter (parity with the prior
                    # ``record_round(rounds=rounds)`` which set rounds outside
                    # the structured-conversion path). Keep the existing
                    # transcript untouched.
                    try:
                        session.record_messages(
                            messages=list(session.messages),
                            rounds=rounds,
                            now=now,
                        )
                    except Exception:  # noqa: BLE001 — never block finalize
                        pass
            if interrupted:
                session.mark_interrupted(now=now)
            elif had_error:
                session.mark_error(now=now)
            else:
                session.mark_done(rounds=rounds, now=now)
            await self._sub_agent_sessions.save(session)
            # Broadcaster buffer alignment (root-cause fix for the 2×/3×
            # duplicate-transcript bug 2026-07-01): the persisted snapshot
            # now covers every frame published so far. Trim them from the
            # broadcaster buffer so a late subscriber's cursor=0 replay
            # holds ONLY frames the snapshot does not have (the terminal
            # `subagent_done` / `subagent_error` frame published AFTER this
            # finalize is one such case). See
            # `SubAgentStreamBroadcaster.trim_all_published` for the full
            # rationale.
            bc = self._stream_broadcaster
            if bc is not None:
                bc.trim_all_published(session.id.value)
        except Exception as exc:  # noqa: BLE001 — never block the run
            _log.warning(
                "chat.agent_tool.session.finalize_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                subagent_id=session.id.value,
                last_prompt_tokens=session.last_prompt_tokens,
            )
        return session.id.value

    def _truncate_sub_tool_result(
        self,
        tool_result: Any,
        *,
        tool_name: str,
        model_hint: str | None,
    ) -> Any:
        """Truncate one tool result via the SHARED model-aware truncator.

        A :class:`BaseException` (from ``asyncio.gather(return_exceptions)``)
        passes through UNTOUCHED so the downstream
        :func:`_agentic_kernel.build_tool_reply_blocks` renders the
        ``[tool_error] ...`` string verbatim (V1 chat_handler.py:2315-2316).

        When a :class:`ToolResultTruncatorPort` is wired, the result is sliced
        to the SAME model-aware budget the parent agentic loop uses (so the
        sub-agent and the parent truncate identically — single tunable cap).
        When unwired (legacy unit stubs), a coarse head char-cap keeps those
        callers working without overflowing the next round's prompt.
        """
        if isinstance(tool_result, BaseException):
            return tool_result
        already_truncated = _kernel_tool_already_truncated(tool_result)
        text = str(tool_result)
        if self._tool_result_truncator is not None:
            truncated, _was_truncated, _orig = _kernel_truncate_tool_result(
                self._tool_result_truncator,
                model_hint=model_hint,
                tool_name=tool_name,
                result_text=text,
                already_truncated=already_truncated,
            )
            return truncated
        # Fallback: no truncator wired — coarse head cap (legacy stubs).
        cap = self._fallback_tool_output_max_chars
        if cap <= 0 or len(text) <= cap:
            return text
        omitted = len(text) - cap
        # Give an ACTIONABLE recovery hint keyed on the tool's output shape
        # instead of a bare char count. Ordered-slice tools (read/list/skill)
        # recover via ``offset``; the kept head's newline count is a faithful
        # lower bound on the next offset to jump past. Other tools recover by
        # narrowing their arguments (pattern/path/limit).
        if tool_name in ("read", "list", "skill"):
            kept_lines = text[:cap].count("\n") + 1
            next_offset = kept_lines + 1
            advice = (
                f"call `{tool_name}` again with offset={next_offset} "
                f"(and optionally a smaller `limit`) to read the remaining "
                f"{omitted} character(s)"
            )
        else:
            advice = (
                "refine the tool call (narrow the pattern / path / limit) to "
                "return only what you need"
            )
        return (
            f"{text[:cap]}\n"
            f"... [tool output truncated after {cap} chars; {omitted} more "
            f"character(s) omitted — {advice}]"
        )

    async def _reduce_spawn_events_to_string(
        self,
        event_iter: AsyncIterator[SubAgentEvent],
    ) -> str:
        """Consolidate a nested sub-agent event stream into ONE result string.

        Alpha unified-spawn-path helper. When a sub-agent's LLM emits an
        ``agent`` tool call, :meth:`_execute_tool_call` re-enters
        :meth:`iter_events` recursively (same code path the main agent uses),
        then must return the nested run's final text back to the calling
        sub-agent's LLM as the tool result string. That reducer is the same
        pattern already used by :meth:`execute` (the backwards-compat text
        wrapper) — one shared helper (§3.6
        cohesion) keeps every "event stream → string" caller in lock step:

        * ``subagent_output`` fragments accumulate as fallback text;
        * ``subagent_done``'s ``result`` wins when present (the terminal
          consolidated text);
        * ``subagent_error`` short-circuits to a labelled ``[sub-agent error:
          …]`` string (never raises — a nested failure must not crash the
          calling sub-agent's round);
        * ``subagent_id`` from the terminal ``done`` frame is prefixed on
          success so the calling sub-agent can resume the nested run
          (``resume_subagent_id``), parity with the main agent's agent-tool
          result line.
        """
        text_parts: list[str] = []
        final_text: str | None = None
        final_id: str | None = None
        try:
            async for event in event_iter:
                etype = event.get("type")
                if etype == "subagent_output":
                    text_parts.append(str(event.get("content", "")))
                elif etype == "subagent_done":
                    final_text = str(event.get("result", "")) or None
                    sid = event.get("subagent_id")
                    if isinstance(sid, str) and sid:
                        final_id = sid
                elif etype == "subagent_error":
                    return (
                        f"[sub-agent error: "
                        f"{event.get('message', 'unknown')}]"
                    )
        except Exception as exc:  # noqa: BLE001 — never crash the caller
            return f"[sub-agent error: {exc}]"
        result = final_text if final_text is not None else "".join(text_parts)
        result = result.strip() or "[sub-agent produced no output]"
        if final_id is not None:
            return f"subagent_id: {final_id}\n{result}"
        return result

    async def _execute_tool_call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        tab_id: TabId,
        conversation_id: ConversationId,
        profile: AgentProfile | None = None,
        allow_spawn: bool = False,
        spawn_model_hint: str | None = None,
        spawn_tool_mode: str | None = None,
        spawn_workspace_root: str | None = None,
        disabled_tools: frozenset[str] = frozenset(),
        guardrail: GuardrailPort | None = None,
        spawn_depth: int = 1,
        current_subagent_id: SubAgentSessionId | None = None,
        current_parent_round_index: int | None = None,
        parent_abort_check: Callable[[], bool] | None = None,
        parent_consume_cancel_tool: Callable[[str], bool] | None = None,
        on_partial: Callable[[str], None] | None = None,
    ) -> str:
        """Execute a single tool call.

        ``on_partial`` (L3 enhancement, tail-appended §3.1): when provided and
        the tool supports streaming (``invoke_streaming`` on the executor), each
        STDOUT/STDERR chunk is forwarded to this callback as it arrives so the
        caller can emit live ``subagent_tool_partial`` events to the spectator
        (父子统一 with the takeover path's exec streaming). Non-streaming tools
        and callers that pass ``None`` are byte-for-byte unchanged.

        The ``agent`` (spawn) tool is BLOCKED by default (recursion guard) and
        permitted ONLY when ``allow_spawn=True`` (the per-tab spawn switch is
        on for this sub-agent run). When permitted, this re-enters
        :meth:`iter_events` recursively with ``spawn_depth+1`` — the SAME code
        path the main agent uses to spawn a first-level sub-agent, no separate
        ``_spawn_grand_sub_agent`` branch. Recursion is capped by
        ``self._max_spawn_depth`` (from ``Settings.chat_max_spawn_depth``): the
        moment a call would exceed the ceiling this method returns a
        diagnostic ``[error: max sub-agent nesting depth (N) reached]`` string
        instead of recursing. The per-level user opt-in (``allow_spawn`` on
        each nested run) is orthogonal — it still controls whether the caller
        can create direct children at all — but no longer doubles as a hard
        recursion guard (a code-level ``allow_spawn=False`` at the grand level
        used to cap recursion at exactly 2 layers with no per-level knob).

        If no ``tool_executor`` was provided at construction time, falls
        back to placeholder results for backward compatibility.

        ``profile`` (V2 enhancement) is a SECOND, handler-layer gate: even
        though a restricted profile (e.g. ``explore``) never advertises a
        denied tool's schema, this guard rejects a denied tool by NAME at the
        dispatch site too (defence in depth — a model that hallucinates a
        denied tool, or a stale tool_call from a prior round, can never mutate
        state through an explore sub-agent). ``None`` / ``GENERAL`` impose no
        extra restriction (regression-safe).

        ``guardrail`` (P9) — when supplied (the DI-wired
        ``guardrail_factory`` produced a fresh instance for this sub-agent
        run), :meth:`GuardrailPort.check` runs BEFORE invocation and
        :meth:`GuardrailPort.observe` runs AFTER, mirroring the main
        agent's :meth:`StreamChatUseCase._execute_single_tool_call`. A
        ``BLOCK`` verdict short-circuits with a ``[guardrail_blocked] {reason}``
        result so the LLM sees the same signal a profile deny would
        emit. When the guardrail is unwired the legacy path
        (profile deny-list + per-session disabled-tools) remains
        authoritative.

        ``spawn_depth`` (alpha unified-spawn-path — recursion counter): the
        depth of the sub-agent running THIS ``_execute_tool_call`` invocation.
        A first-level sub-agent's tool executor passes ``spawn_depth=1``; a
        grand sub-agent passes 2; and so on. The nested ``agent`` recursion
        below passes ``spawn_depth + 1`` down to :meth:`iter_events`.
        ``current_subagent_id`` (also alpha) is the id of the sub-agent
        session running THIS call — threaded onto the nested spawn's
        ``parent_subagent_id`` so the tree edge is persisted; ``None`` when
        this handler is running without a repo wired (legacy/stub parity).
        ``current_parent_round_index`` is the caller's round number so a
        nested spawn's SUBAGENT_START frame can carry ``round`` for
        broadcaster fan-out (parity with the main agent's dispatch).
        """
        # Per-session ("this conversation only") EXECUTION gate (V2 enhancement,
        # inherited from the parent turn). The user turned this tool OFF for the
        # conversation via the SessionToolsPopover; the sub-agent advertise
        # filter (``_sub_agent_tool_schemas``) already hides it, but this is the
        # AUTHORITATIVE handler-layer backstop — even a hallucinated / stale
        # tool_call for a disabled tool is refused without executing. Distinct
        # ``[tool_disabled]`` signal (not an error) so the sub-agent's LLM adapts.
        if tool_name in disabled_tools:
            return (
                f"[tool_disabled] tool {tool_name!r} is turned off for this "
                f"conversation (per-session setting)"
            )

        # The ``agent`` (spawn) tool: blocked by default (the per-level user
        # opt-in). When spawning is permitted for THIS sub-agent run, re-enter
        # :meth:`iter_events` recursively with ``spawn_depth+1`` — the SAME
        # code path the main agent uses. Recursion 封顶 = ``self._max_spawn_depth``
        # (from Settings). State-mutation safety: an EXPLORE (read-only) profile
        # must never spawn (defence in depth alongside the profile deny-list).
        if tool_name == "agent":
            if not allow_spawn:
                return "[error: sub-agents cannot spawn further sub-agents]"
            if profile is not None and profile is not GENERAL:
                return (
                    f"[guardrail_blocked] the '{profile.name}' sub-agent "
                    f"profile cannot spawn sub-agents"
                )
            # Recursion ceiling — the ONLY hard cap now that
            # ``_spawn_grand_sub_agent`` is gone. Checked BEFORE building an
            # invocation request so a nested LLM that keeps calling ``agent``
            # at the ceiling gets a fast, cheap diagnostic instead of another
            # spawn attempt.
            if spawn_depth >= self._max_spawn_depth:
                return (
                    f"[error: max sub-agent nesting depth "
                    f"({self._max_spawn_depth}) reached — refuse to spawn "
                    "another level. Continue the task inline or return a "
                    "consolidated answer to your parent.]"
                )
            description = arguments.get("description", "") or arguments.get(
                "prompt", ""
            )
            if not isinstance(description, str) or not description:
                return "[sub-agent error: 'description' argument is required]"
            raw_resume = arguments.get("resume_subagent_id")
            resume_id: SubAgentSessionId | None = None
            if isinstance(raw_resume, str) and raw_resume:
                try:
                    resume_id = SubAgentSessionId(raw_resume)
                except (ValueError, TypeError):
                    resume_id = None
            raw_type = arguments.get("subagent_type")
            nested_subagent_type = (
                raw_type if isinstance(raw_type, str) else None
            )
            inv_request = ToolInvocationRequest(
                tab_id=tab_id,
                conversation_id=conversation_id,
                tool_name="agent",
                arguments=dict(arguments),
            )
            # Unified spawn call: SAME ``iter_events`` the main agent invokes,
            # only the counters advance. ``allow_spawn`` is threaded from the
            # nested spawn's ``arguments`` — if the caller (the sub-agent's
            # LLM) explicitly asked its child to spawn too, honour that; else
            # default to False (a spawning sub-agent must OPT its own child in
            # per the per-level design). Deeper recursion is still bounded by
            # ``self._max_spawn_depth`` regardless of ``allow_spawn`` votes.
            nested_allow_spawn = bool(arguments.get("allow_spawn", False))
            return await self._reduce_spawn_events_to_string(
                self.iter_events(
                    inv_request,
                    model_hint=spawn_model_hint,
                    tool_mode=spawn_tool_mode,
                    workspace_root=spawn_workspace_root,
                    resume_session_id=resume_id,
                    subagent_type=nested_subagent_type,
                    allow_spawn=nested_allow_spawn,
                    disabled_tools=disabled_tools,
                    spawn_depth=spawn_depth + 1,
                    parent_subagent_id=current_subagent_id,
                    parent_round_index=current_parent_round_index,
                    # Fix 2: propagate the parent-Stop predicate down the whole
                    # spawn tree so a grand / great-grand sub-agent also stops
                    # the instant the originating tab's ⏹ lands.
                    parent_abort_check=parent_abort_check,
                    # Single-tool cancel: propagate the parent tab's per-tool
                    # cancel check down the whole spawn tree so a nested
                    # sub-agent's own tool card stop is honoured too (parity with
                    # ``parent_abort_check``). The check is keyed by ``call_id``,
                    # so it only fires for the exact call the user stopped.
                    parent_consume_cancel_tool=parent_consume_cancel_tool,
                ),
            )

        # Profile deny-list gate (defence in depth). A denied tool is refused
        # with a clear, non-fatal signal so the LLM understands why and adapts
        # (it is NOT a tool error — the tool simply isn't available to this
        # profile). EXPLORE denies every state-mutating tool, enforcing its
        # strictly read-only contract regardless of what reached the dispatch.
        if (
            profile is not None
            and profile is not GENERAL
            and tool_name in profile.denied_tools
        ):
            # Prefix with ``[guardrail_blocked]`` so the round skeleton's
            # ok-judgement flags it as NOT ok (the same treatment a permission
            # guardrail block gets) — the UI renders an error-state panel and
            # the model sees a clear "this tool is unavailable" signal.
            return (
                f"[guardrail_blocked] tool '{tool_name}' is not available to "
                f"the '{profile.name}' sub-agent profile — this profile is "
                f"read-only and cannot modify files or system state"
            )

        # Fallback to placeholder when no executor is wired.
        if self._tool_executor is None:
            return (
                f"[tool '{tool_name}' executed with args: "
                f"{json.dumps(arguments, ensure_ascii=False)[:200]}]"
            )

        # P9 — guardrail check (BEFORE invocation). Mirrors the main
        # agent's ``_execute_single_tool_call`` (streaming.py:7009): a
        # ``BLOCK`` verdict bypasses invocation and surfaces a
        # ``[guardrail_blocked] {reason}`` result so the LLM understands
        # why the tool was refused; ``observe`` still fires with
        # ``success=False`` so the controller sees the rejection. Without
        # a guardrail the path degrades to the pre-P9 behaviour.
        if guardrail is not None:
            verdict = guardrail.check(
                tool_name=tool_name, arguments=arguments,
            )
            if verdict.decision is GuardrailDecision.BLOCK:
                guardrail.observe(
                    tool_name=tool_name,
                    arguments=arguments,
                    result=f"[guardrail_blocked] {verdict.reason}",
                    success=False,
                )
                return f"[guardrail_blocked] {verdict.reason}"

        # Dispatch to the real tool registry.
        request = ToolInvocationRequest(
            tab_id=tab_id,
            conversation_id=conversation_id,
            tool_name=tool_name,
            arguments=arguments,
        )

        # L3 — streaming exec (父子统一 with the takeover path): when the caller
        # wants live partials AND the executor exposes ``invoke_streaming`` (a
        # tail-appended optional capability on ``ToolInvocationPort`` — see
        # ``application.ports``), consume the chunk stream and forward each
        # STDOUT/STDERR increment through ``on_partial`` (the ``_gen`` driver
        # puts a ``ToolExecutionItem(partial=True, delta=...)`` on its side
        # queue → the kernel emits ``KernelToolPartial`` → the event adapter's
        # L1 branch turns it into a ``subagent_tool_partial`` event so the
        # spectator streams the tool card's output live, identical to the
        # takeover path). The terminal DONE chunk carries the FINAL result the
        # LLM sees — we drop through to the normal ok/err handling below with
        # ``invocation_result`` synthesised from DONE, so per-result truncation
        # / guardrail observe / error path stay byte-for-byte identical to the
        # non-streaming path. Non-streaming tools / callers with no
        # ``on_partial`` fall through to the unchanged one-shot ``invoke``.
        invocation_result: Any = None
        _stream_factory = getattr(self._tool_executor, "invoke_streaming", None)
        if on_partial is not None and _stream_factory is not None:
            _chunk_iter: AsyncIterator[ToolStreamChunk] | None = None
            try:
                _maybe = _stream_factory(request)
                _chunk_iter = (
                    _maybe if isinstance(_maybe, AsyncIterator) else None
                )
            except Exception:  # noqa: BLE001 — defensive: fall back to one-shot
                _chunk_iter = None
            if _chunk_iter is not None:
                # ``aclosing`` guarantees the underlying exec stream is torn
                # down (subprocess tree-kill) if this coroutine is cancelled
                # mid-stream — same ORPHAN-KILL guard the takeover path uses.
                _done_chunk: ToolStreamChunk | None = None
                _stream_ok = True
                _stream_err: BaseException | None = None
                try:
                    async with contextlib.aclosing(_chunk_iter) as _guarded:
                        async for _ch in _guarded:
                            if _ch.kind is ToolStreamChunkKind.DONE:
                                _done_chunk = _ch
                                break
                            if not _ch.text:
                                continue
                            # Forward the increment to the caller's spectator
                            # publish path. Wrapped in try/except so a fan-out
                            # error never poisons the exec stream.
                            try:
                                on_partial(_ch.text)
                            except Exception:  # noqa: BLE001 — spectator fan-out is best-effort
                                pass
                except Exception as _exc:  # noqa: BLE001 — surface as failed
                    _stream_ok = False
                    _stream_err = _exc
                # Synthesize an ``invocation_result``-shaped object so the tail
                # (ok/err + guardrail.observe + return_text) stays identical to
                # the non-streaming branch below.

                @dataclass(slots=True)
                class _StreamedInvocationResult:
                    ok: bool
                    result: Any = None
                    error_message: str | None = None
                    error_code: str | None = None

                if not _stream_ok:
                    invocation_result = _StreamedInvocationResult(
                        ok=False,
                        error_message=f"{_stream_err}",
                        error_code="tool_stream_exception",
                    )
                elif _done_chunk is not None:
                    invocation_result = _StreamedInvocationResult(
                        ok=bool(_done_chunk.ok),
                        result=_done_chunk.result if _done_chunk.ok else None,
                        error_message=(
                            None
                            if _done_chunk.ok
                            else (
                                str(_done_chunk.result)
                                if _done_chunk.result is not None
                                else "tool stream ended without a result"
                            )
                        ),
                        error_code=(
                            None if _done_chunk.ok else "tool_stream_error"
                        ),
                    )
                else:
                    # Stream ended WITHOUT a DONE chunk — same
                    # State-Truth-First stance as the takeover path: never
                    # fabricate an empty success. Surface as failed so the LLM
                    # sees the tool did not actually complete.
                    invocation_result = _StreamedInvocationResult(
                        ok=False,
                        error_message=(
                            "tool stream ended without a result"
                        ),
                        error_code="tool_stream_incomplete",
                    )

        if invocation_result is None:
            invocation_result = await self._tool_executor.invoke(request)

        if invocation_result.ok:
            # Coerce result to string for the message history.
            raw = invocation_result.result
            if isinstance(raw, str):
                result_text = raw
            else:
                result_text = json.dumps(raw, ensure_ascii=False, default=str)
            if guardrail is not None:
                guardrail.observe(
                    tool_name=tool_name,
                    arguments=arguments,
                    result=result_text,
                    success=True,
                )
            return result_text

        # Tool failed — return structured error so the LLM can adapt.
        err_text = (
            f"[tool '{tool_name}' error: "
            f"{invocation_result.error_message or invocation_result.error_code}]"
        )
        if guardrail is not None:
            guardrail.observe(
                tool_name=tool_name,
                arguments=arguments,
                result=err_text,
                success=False,
            )
        return err_text
