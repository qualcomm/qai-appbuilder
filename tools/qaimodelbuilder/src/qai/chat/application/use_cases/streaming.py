# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Streaming chat use cases (StreamChat + StopChat).

These two use cases are the heart of the chat bounded context: they
implement the multi-tab parallel conversation model from
``refactor-plan.md`` §10 plus, since PR-401c (S7.5 lane L4), the
agentic-loop behaviour migrated from the legacy
``backend/chat_handler.py`` (3368 LOC, 2026-05-30 snapshot):

* prompt-too-long / throttling **retry with backoff**
  (replaces ``backend/chat_handler.py:476-540``);
* in-stream **tool-call execution + followup rounds**
  (replaces ``backend/chat_handler.py:696-860``) — gated behind the
  optional :class:`GuardrailPort` / :class:`ToolResultTruncatorPort`
  injections so legacy callers without these ports retain the
  PR-033 single-turn behaviour byte-for-byte.

:class:`StreamChatUseCase`
    Orchestrates a streaming turn: persists the user message, opens
    the LLM stream, retries on transient failures, optionally executes
    tool calls inline (with guardrail + adaptive truncation), runs up
    to ``max_followup_rounds`` follow-up LLM rounds with the tool
    results fed back into the prompt, and finalises the assistant
    message when the stream ends or aborts.

:class:`StopChatUseCase`
    Signals an in-flight stream to abort cooperatively, going through
    :class:`StreamAbortRegistryPort`.

Multi-tab semantics:

* Two distinct conversations may stream in parallel; the registry
  partitions handles by :class:`TabId` and never blocks across tabs.
* Two tabs cannot stream the *same* conversation simultaneously --
  :class:`StreamAbortRegistryPort.register` raises
  :class:`ConversationLockedError` when a second register attempt
  arrives for a conversation that is already streaming through some
  other tab.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Mapping
from pathlib import Path
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable

from qai.chat.application.ports import (
    BudgetTrackerPort,
    ContextCompressionPort,
    ConversationRepositoryPort,
    CodePersonaResolverPort,
    CompactionCheckpointStorePort,
    ExperienceRecallPort,
    GuardrailDecision,
    GuardrailPort,
    HookEnginePort,
    HookFiredRecord,
    ImageUploadStorePort,
    InjectionRegistryPort,
    LLMStreamPort,
    LLMStreamRequest,
    PromptSnapshot,
    PromptSnapshotStorePort,
    PromoteReadyScanPort,
    RetryCategory,
    RetryPolicyPort,
    StreamAbortHandle,
    StreamAbortRegistryPort,
    SubAgentEventStreamPort,
    SubAgentSessionRepositoryPort,
    SystemPromptBuilderPort,
    SystemPromptRequest,
    SystemPromptResult,
    TabSessionStorePort,
    ToolInvocationPort,
    ToolInvocationRequest,
    ToolInvocationResult,
    ToolResultTruncationRequest,
    ToolResultTruncatorPort,
    ToolStreamChunk,
    ToolStreamChunkKind,
)
from qai.chat.domain.content import MessageContent, MessageRole
from qai.chat.domain.hook import HookDecision, HookEvent
from qai.chat.domain.errors import (
    ChatStreamAbortedError,
    ConversationLockedError,
    SubAgentSessionConflictError,
)
from qai.chat.domain.events import (
    ChatStreamAbortedEvent,
    ChatStreamCompletedEvent,
    ChatStreamFrameEvent,
    ChatStreamStartedEvent,
    MessageAppendedEvent,
)
from qai.chat.domain.ids import (
    ConversationId,
    MessageId,
    SubAgentSessionId,
    TabId,
)
from qai.chat.domain.message import Message
from qai.chat.domain.history_trim import trim_messages_by_rounds
from qai.chat.domain.model_profiles import get_context_limit
from qai.chat.domain.stream_frame import StreamFrame, StreamFrameType
from qai.chat.domain.tab import TabStatus
from qai.chat.domain.usage_math import finalize_cumulative_prompt_usage
from qai.chat.application.use_cases._agentic_kernel import (
    COMPRESS_PRESERVE_TAIL as _COMPRESS_PRESERVE_TAIL,
    COMPRESS_TARGET_RATIO as _COMPRESS_TARGET_RATIO,
    INTER_ROUND_COMPRESS_THRESHOLD_RATIO as _INTER_ROUND_COMPRESS_THRESHOLD_RATIO,
    CompactionCheckpoint as _CompactionCheckpoint,
    age_old_tool_outputs,
    AGED_TOOL_OUTPUT_PLACEHOLDER,
    build_assistant_tool_calls_block as _kernel_assistant_tool_calls_block,
    build_cancelled_tool_message as _build_cancelled_tool_message,
    build_tool_reply_blocks as _kernel_tool_reply_blocks,
    is_self_contained_agent_hint as _is_self_contained_agent,
    tool_result_already_truncated as _kernel_tool_already_truncated,
    TOOL_CALLS_CONTENT_SENTINEL,
)
from qai.chat.application.use_cases._compaction_engine import (
    CompactionCheckpointEngine,
)
from qai.chat.application.provider_cache_capability import (
    ProviderCacheCapabilityRegistry,
)
from qai.chat.application._token_estimate_helpers import (
    _last_assistant_with_usage,
    append_display_usage_fields as _append_display_usage_fields_shared,
    assistant_eff_prompt as _assistant_eff_prompt,
    effective_prompt_tokens as _effective_prompt_tokens,
    precise_text_tokens as _precise_text_tokens,
    record_subagent_turn_usage as _record_subagent_turn_usage,
)
from qai.chat.application.use_cases._image_refs import (
    extract_image_refs as _extract_image_refs,
    resolve_image_refs_to_vision_blocks as _resolve_image_refs_to_vision_blocks,
)
from qai.chat.application.use_cases._single_agent_turn import (
    InjectedContent,
    KernelAborted,
    KernelChunk,
    KernelError,
    KernelFinished,
    KernelMaxRoundsReached,
    KernelRoundStarted,
    KernelStreamPassthrough,
    KernelToolCallSeen,
    KernelToolCallsIssued,
    KernelToolPartial,
    KernelToolResult,
    RoundEndDecision,
    SingleAgentTurnKernel,
    ToolExecutionItem,
)
from qai.chat.application.use_cases._streaming_subagent_frames import (
    SUBAGENT_FRAME_TYPES as _SUBAGENT_FRAME_TYPES,
    accumulate_sub_agent_block as _accumulate_sub_agent_block,
    build_assistant_meta as _build_assistant_meta,
    drop_trailing_current_user as _drop_trailing_current_user,
    iter_subagent_blocks as _iter_subagent_blocks,
    now_ms as _now_ms,
    subagent_event_to_frame as _subagent_event_to_frame,
)
from qai.chat.application.use_cases._stream_guards import (
    abortable_frames as _shared_abortable_frames,
    is_meaningful_stream_frame as _shared_is_meaningful_stream_frame,
    network_retrying_stream as _shared_network_retrying_stream,
)
from qai.chat.application.use_cases._streaming_helpers import (
    TURN_WARNING_START as _TURN_WARNING_START,
    TURN_WARNING_STEP as _TURN_WARNING_STEP,
    assemble_multimodal_messages as _assemble_multimodal_messages_fn,
    build_message as _build_message_fn,
    build_synthetic_retry_history as _build_synthetic_retry_history_fn,
    build_tool_call_message as _build_tool_call_message_fn,
    classify_error_frame as _classify_error_frame_fn,
    compute_turn_warning_threshold as _compute_turn_warning_threshold_fn,
    detect_effective_mode as _detect_effective_mode_fn,
    rebuild_history_wire_messages as _rebuild_history_wire_messages,
    repair_orphan_tool_messages as _repair_orphan_tool_messages,
    wire_to_structured_messages as _wire_to_structured_messages,
)
from qai.chat.application.use_cases._workspace_context import (
    WORKSPACE_CONTEXT_EXTRA_KEY,
    resolve_workspace_context_files as _resolve_workspace_context_files_fn,
)
from qai.chat.application.use_cases.tool_advertise import (
    CONDITIONAL_TOOL_NAMES as _CONDITIONAL_TOOL_NAMES,
    SUB_AGENT_EXCLUDED_TOOLS as _SUB_AGENT_EXCLUDED_TOOLS,
    compose_advertised_tools as _compose_advertised_tools,
    schema_tool_name as _shared_schema_tool_name,
)
from qai.platform.events import EventBus
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger
from qai.platform.scheduling.tool_concurrency import ToolConcurrencyManager
from qai.platform.time import Clock

_log = get_logger(__name__)


@contextlib.asynccontextmanager
async def _null_async_ctx() -> "AsyncIterator[None]":
    """No-op async context manager (used when no concurrency budget is wired)."""
    yield None


# Default upper bound on follow-up rounds when the use case is wired with the
# PR-401c agentic-loop ports.  Mirrors the legacy
# ``backend/chat_handler.py:_max_iterations = 25`` cap; the chat use case
# may be re-instantiated with a smaller number for non-agent surfaces
# (OpenAI Compat, channels) by setting :attr:`StreamChatUseCase` constructor
# kwarg ``max_followup_rounds`` explicitly.  ``0`` disables the loop entirely
# (default — matches PR-033 single-turn behaviour).
DEFAULT_MAX_FOLLOWUP_ROUNDS: int = 0
LEGACY_MAX_FOLLOWUP_ROUNDS: int = 25

# No-progress circuit breaker (Fix B): if the agentic follow-up loop issues
# the SAME tool-call batch (identical tool names + args) this many times in a
# row, it is stuck (not converging) → break with a graceful END instead of
# spinning up to ``max_followup_rounds``. ``3`` tolerates a single legitimate
# retry while catching a true loop quickly.
_NO_PROGRESS_LIMIT: int = 3

# How long pre-send context compaction may run before the use case surfaces a
# transient ``COMPACTION_PROGRESS`` ("compressing context…") frame to the UI.
# Compaction is usually ~100ms (pure trim) but a turn that also runs the Level 2
# LLM summary can take seconds; only those slow compactions cross this gate and
# show the indicator, so the common (fast) case yields no extra frames.
_COMPACTION_PROGRESS_THRESHOLD_S: float = 2.0


def _is_meaningful_stream_frame(frame: StreamFrame) -> bool:
    """True when ``frame`` carries real progress (resets the stall watchdog).

    Thin shim delegating to
    :func:`qai.chat.application.use_cases._stream_guards.is_meaningful_stream_frame`
    — the SAME function the sub-agent loop now uses. Single source of
    truth (P4: shared LLM-stream guards across main agent + sub-agent).
    """
    return _shared_is_meaningful_stream_frame(frame)


def _tool_calls_signature(frames: "list[StreamFrame]") -> str:
    """Stable signature of a round's pending tool calls (name + args).

    Two rounds with the SAME signature requested structurally identical tool
    calls. Used by the no-progress circuit breaker to detect a stuck loop.
    Order-independent (sorted) so a reordered-but-identical batch still
    matches; returns ``""`` for an empty batch (never counted as a repeat).
    """
    parts: list[str] = []
    for fr in frames:
        payload = getattr(fr, "payload", None)
        if not isinstance(payload, dict):
            continue
        name = payload.get("tool_name") or ""
        # Model-emitted TOOL_CALL frames carry args under ``arguments``;
        # internally re-emitted call frames may use ``args``. Accept either so
        # the signature reflects the real call arguments (a wrong key here
        # would make every round look identical → false-positive break).
        args = payload.get("arguments")
        if args is None:
            args = payload.get("args")
        try:
            args_str = json.dumps(
                args if isinstance(args, dict) else {},
                ensure_ascii=False,
                sort_keys=True,
            )
        except (TypeError, ValueError):
            args_str = "{}"
        parts.append(f"{name}\x1f{args_str}")
    parts.sort()
    return "\x1e".join(parts)


# Frame types whose grouping the frontend keys off ``round_index`` (the
# 0-based agentic-loop round).  Only CHUNK / TOOL_CALL / TOOL_RESULT carry
# the per-round text↔tool-card binding the frontend renders interleaved;
# control frames (END / ERROR / TURN_WARNING / TOOL_MODE_CHANGED /
# SUBAGENT_* / AGENT_SUMMARY) are round-agnostic and left untouched so the
# wire stays byte-for-byte identical for them.
_ROUND_STAMPED_FRAME_TYPES: frozenset[StreamFrameType] = frozenset(
    {
        StreamFrameType.CHUNK,
        StreamFrameType.REASONING,
        StreamFrameType.TOOL_CALL,
        StreamFrameType.TOOL_RESULT,
    },
)


def _stamp_round(frame: StreamFrame, round_index: int) -> StreamFrame:
    """Return ``frame`` stamped with ``round_index`` (or unchanged).

    The single round-authority helper: the use case is the *only* layer
    that knows the agentic-loop round structure (adapters mint frames with
    no loop notion — see :meth:`StreamFrame.with_round_index`). Stamping
    happens at the use-case forwarding boundary so the frontend can group
    strictly by ``round_index`` with **zero inference** (no more "lead_in
    non-empty ⇒ new round" heuristic that mis-ordered same-round
    inter-tool narration). Only CHUNK / REASONING / TOOL_CALL / TOOL_RESULT
    are stamped (``_ROUND_STAMPED_FRAME_TYPES``); other frame types pass
    through unchanged so their wire shape is preserved. REASONING is stamped
    so the model's thinking binds to the SAME per-round assistant message as
    that round's answer CHUNK (frontend renders the collapsible thinking block
    above the answer in one bubble, instead of splitting into two).
    """
    if frame.frame_type in _ROUND_STAMPED_FRAME_TYPES:
        return frame.with_round_index(round_index)
    return frame


def _read_round_index(payload: Mapping[str, Any]) -> int | None:
    """Return the integer ``round_index`` from a frame payload, or ``None``.

    Mirrors the frontend ``readRoundIndex`` (``frameHandlers.ts``): the value
    is the snake_case ``round_index`` key the adapter / use case stamp. Guards
    against ``bool`` (a stray ``True`` is not a valid round index).
    """
    ri = payload.get("round_index")
    if isinstance(ri, int) and not isinstance(ri, bool):
        return ri
    return None


def _stamp_request_id(
    frame: StreamFrame, request_id: str | None,
) -> StreamFrame:
    """Return ``frame`` with its round's prompt-snapshot id stamped.

    Per-round prompt-snapshot wiring (V1 parity): every agentic round saves
    its OWN snapshot of the messages sent that round, and the messages it
    produces (assistant text + tool cards) carry that round's
    ``request_id`` — different rounds → different ``request_id`` →
    different prompts when each tool card's 📄 button is clicked (V1
    ``useChat.js``: each ``/api/chat`` round's ``done`` frame carries its
    request_id; that round's ``assistant``/``assistant{tool_calls}``/
    ``tool`` messages all share it).

    Only stamped on round-bearing frames (CHUNK / TOOL_CALL / TOOL_RESULT —
    the same set as :func:`_stamp_round`) so the END / ERROR / SUBAGENT_*
    wire shape stays byte-for-byte unchanged.  ``request_id`` is an
    **optional appended payload field** (AGENTS.md §3.1 — payloads only
    grow at the tail; existing keys untouched).  ``None`` is a no-op.
    """
    if request_id is None or not isinstance(request_id, str):
        return frame
    if frame.frame_type not in _ROUND_STAMPED_FRAME_TYPES:
        return frame
    new_payload = dict(frame.payload)
    new_payload["request_id"] = request_id
    return StreamFrame(
        frame_id=frame.frame_id,
        frame_type=frame.frame_type,
        sequence=frame.sequence,
        payload=new_payload,
    )


_STAMPABLE_FRAME_TYPES = frozenset(
    {
        StreamFrameType.TOOL_CALL,
        # SUBAGENT_TOOL carries the wall-clock for the sub-agent tool card's
        # unmount-survival elapsed (parity with the main agent's TOOL_CALL
        # path). Without stamping, ``ToolExecPanel`` falls back to a
        # remount-local ``performance.now()`` ref and the elapsed timer resets
        # to 00:00 every time the user switches browser tabs (the reported
        # sub-agent tool card elapsed-reset bug). See sibling fold in
        # ``_streaming_subagent_frames.accumulate_sub_agent_block``.
        StreamFrameType.SUBAGENT_TOOL,
    }
)


def _stamp_emitted_at(frame: StreamFrame, emitted_at_ms: int) -> StreamFrame:
    """Return ``frame`` stamped with its real wall-clock emit time (ms epoch).

    V1 stamps every assistant / tool message with its OWN ``Date.now()`` at
    the moment it is produced (``useChat.js:2461-2465`` per round,
    ``useChat.js:2694-2722`` final text), so a reloaded history shows each
    message's *real* generation time. V2 instead rebuilds all the turn's
    messages at turn completion from accumulated frames, so without a
    per-message timestamp every round message would get the single
    turn-completion ``now`` — making every reloaded card show the SAME time
    (the bug). Capturing the real emit time on each round's TOOL_CALL frame
    (when it is collected for persistence) lets ``build_tool_call_message``
    give each round message its own ``created_at`` (V1 parity) instead of the
    shared turn-completion time.

    ``emitted_at_ms`` is an **optional appended payload field** (AGENTS.md
    §3.1 — payloads only grow at the tail; existing keys untouched), so the
    wire frame stays byte-for-byte backward compatible. Stamped on the
    frame types in :data:`_STAMPABLE_FRAME_TYPES`:

    * ``TOOL_CALL`` — main-agent round-boundary carrier
      ``build_tool_call_message`` keys off, AND the frontend's
      ``ToolExecPanel`` reads via ``ChatToolCall.ts`` for unmount-survival
      elapsed (resists browser-tab switch / scroll-out remounts).
    * ``SUBAGENT_TOOL`` — sub-agent counterpart: lets the sub-agent tool
      card's ``ToolExecPanel`` instance compute elapsed off the upstream
      wall-clock instead of a remount-local ``performance.now()`` ref.

    Persistence / UI falls back to the turn-completion ``now`` when the field
    is absent (old data / frames minted before this change).
    """
    if frame.frame_type not in _STAMPABLE_FRAME_TYPES:
        return frame
    if "emitted_at_ms" in frame.payload:
        # Already stamped (e.g. re-collected on incremental persist) — keep
        # the first-seen real time, never overwrite with a later one.
        return frame
    new_payload = dict(frame.payload)
    new_payload["emitted_at_ms"] = emitted_at_ms
    return StreamFrame(
        frame_id=frame.frame_id,
        frame_type=frame.frame_type,
        sequence=frame.sequence,
        payload=new_payload,
    )


# ---------------------------------------------------------------------------
# ARCH-1 / A-3 cohesion split (zero behaviour change):
#   * the per-conversation turn-warning constants
#     (``_TURN_WARNING_START`` / ``_TURN_WARNING_STEP``) and the
#     ``compute_turn_warning_threshold`` logic now live in
#     ``_streaming_helpers`` (imported above as ``_TURN_WARNING_*``);
#   * the sub-agent frame-fold / meta pure functions
#     (``_SUBAGENT_FRAME_TYPES`` / ``_now_ms`` /
#     ``_drop_trailing_current_user`` / ``_accumulate_sub_agent_block`` /
#     ``_subagent_event_to_frame`` / ``_build_assistant_meta``) now live in
#     ``_streaming_subagent_frames`` (imported above under the same private
#     aliases).
# They are byte-for-byte identical; only relocated so this orchestrating
# file shrinks below the cohesion advisory ceiling (AGENTS.md §3.6).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Local (on-device) model detection + prompt markers (V1 parity)
# ---------------------------------------------------------------------------
# V1 ``backend/chat_handler.py:400``:
#   ``_is_local_model = is_local or resolved_model_id.startswith("local::")``
# V2 resolves the model via ``request.model_hint`` (``local::<id>`` for
# on-device GenieAPIService models); the cloud path uses provider-qualified
# ids without this prefix.
_LOCAL_MODEL_HINT_PREFIX = "local::"

# V1 ``backend/local_prompt_builder.py:93`` — the marker that makes
# GenieAPIService's PromptOptimizer.DetectAgentType() take the MAIN_AGENT
# optimisation path.
_LOCAL_AGENT_MAIN_MARKER = "agent=main"

#: Language-follow rule for the on-device (local model) system prompt. The
#: local path assembles its prompt here and does NOT flow through
#: ``RichSystemPromptBuilder``, so the cloud-side ``LANGUAGE_RULE_GUIDANCE``
#: (an adapters-layer constant the application layer must not import) cannot be
#: reused; this is the application-layer counterpart, kept short for on-device
#: context budgets. Prevents the model drifting into Korean/Japanese when the
#: skills metadata is English but the user writes Chinese.
_LOCAL_LANGUAGE_RULE = (
    "LANGUAGE RULE: Always reply in the SAME language the user writes in "
    "(Chinese -> Chinese, English -> English). Never switch to Korean or "
    "Japanese on your own."
)

#: Default model-builder workspace root (kept in sync with
#: ``WorkspaceSettings.model_root`` / ``_workspace_resolver``). Used as the
#: fallback when the injected system-prompt builder carries no resolved
#: workspace value.
_DEFAULT_WORKSPACE_ROOT = "C:/WoS_AI"

#: Workspace project-context (AGENTS.md / CLAUDE.md) constants + reader live in
#: the neutral ``_workspace_context`` module so the main-agent path (here) and
#: the sub-agent path (``agent_tool.py``) share ONE implementation (single
#: source of truth — same rationale as ``_agentic_kernel``). ONLY cloud models
#: receive these blocks; ``_resolve_workspace_context_files`` below gates on
#: ``model_hint``. The string key ``workspace_context_files`` is a cross-layer
#: convention (same pattern as ``memory_context`` / ``_session_workspace_root``):
#: published here on ``extra`` and read back by the cloud prompt builder via
#: the same literal key — NO symbol import across the layered boundary.
_WORKSPACE_CONTEXT_EXTRA_KEY: str = WORKSPACE_CONTEXT_EXTRA_KEY


def _session_workspace_from_conv(conv: Any) -> str | None:
    """Extract a usable per-session workspace from a conversation's ``meta``.

    Returns ``meta["workspace"]`` when set and non-blank, else ``None`` (the
    sub-agent then falls back to the tool-layer default / global workspace).
    Operates on an already-loaded conversation (the dispatch flow holds it).
    """
    meta = getattr(conv, "meta", None)
    if isinstance(meta, dict):
        ws = meta.get("workspace")
        if isinstance(ws, str) and ws.strip():
            return ws.strip()
    return None


def _extract_model_workdir_from_text(
    text: str,
    workspace_root: str | None = None,
) -> str:
    """Extract the model workspace dir (``<root>\\<model>``) mentioned LAST in
    ``text``, or ``""`` when none is present.

    Backend mirror of the frontend ``extractModelWorkdirFromMessages``
    (``frontend/src/utils/modelWorkdir.ts``): the turn-end promote-ready
    detector scans the assistant's FINAL summary text for a
    ``C:\\WoS_AI\\<model>`` path (the directory the model-conversion / AI-Hub
    download pipeline writes to). The SKILL contract guarantees every round's
    final summary prints this top-level path (user-visible), so the LAST match
    is the model this turn worked on.

    Separator tolerance mirrors the frontend regex: each run of ``\\`` / ``/``
    in ``root`` becomes ``[\\/]+`` (so ``C:\\``, ``C:/`` and the JSON-escaped
    ``C:\\\\`` forms all match); the model dir group is ``[A-Za-z0-9_-]+``. The
    non-separator segments of ``root`` are escaped so a drive letter / dot /
    paren matches literally.
    """
    if not text:
        return ""
    root = (workspace_root or "").strip() or _DEFAULT_WORKSPACE_ROOT
    segments = [seg for seg in re.split(r"[\\/]+", root) if seg]
    if not segments:
        return ""
    sep = r"[\\/]+"
    body = sep.join(re.escape(seg) for seg in segments)
    pattern = re.compile(body + sep + r"([A-Za-z0-9_-]+)")
    matches = list(pattern.finditer(text))
    if not matches:
        return ""
    # LAST match = most recently mentioned (frontend "take the last match").
    model = matches[-1].group(1)
    normalised_root = "\\".join(segments)
    return f"{normalised_root}\\{model}"


def _build_local_workspace_directive(workspace_root: str) -> str:
    """Render the working-directory directive for the LOCAL (on-device) prompt.

    The on-device path builds its own minimal V1-style prompt and does NOT
    go through ``RichSystemPromptBuilder``, so the workspace directive must
    be injected here too — otherwise a local model has no idea a dedicated
    workspace exists and tends to wander the drive (e.g. listing ``C:\\``).
    Mirrors the cloud directive in
    ``qai.chat.adapters.system_prompt_builder._build_workspace_context``;
    kept short for the smaller on-device context window.
    """
    root = (workspace_root or "").strip() or _DEFAULT_WORKSPACE_ROOT
    return (
        "## Working Directory (IMPORTANT)\n"
        f"Your working directory is `{root}`. All file/command tools "
        "default their relative paths and cwd to it. Create and keep your "
        f"files under `{root}` — you may freely recurse inside it (e.g. "
        "`glob` with `**/*`). Only avoid recursive scans rooted at unbounded "
        "drive roots (e.g. `C:\\`) when looking for a place to work."
    )


def _is_local_model_hint(model_hint: str | None) -> bool:
    """Return True when ``model_hint`` targets a local (on-device) model.

    V1 parity ``backend/chat_handler.py:400`` (``startswith("local::")``).
    """
    return isinstance(model_hint, str) and model_hint.startswith(
        _LOCAL_MODEL_HINT_PREFIX
    )


class _NoopBudgetTracker:
    """Application-layer no-op :class:`BudgetTrackerPort` fallback.

    Used ONLY when ``StreamChatUseCase(budget_tracker=None)`` (legacy / unit
    stubs); production DI always injects a real tracker. Defined HERE (not
    imported from ``qai.chat.adapters.NullBudgetTracker``) so the application
    layer never imports adapters (import-linter layered contract). Every method
    is a benign no-op / disabled result so budgeting is off, byte-for-byte the
    prior behaviour.
    """

    async def observe(
        self, conversation_id: "ConversationId", delta_tokens: int
    ) -> None:
        return None

    async def check(
        self, conversation_id: "ConversationId"
    ) -> "BudgetCheckResult":
        from qai.chat.domain.budget import BudgetCheckResult

        return BudgetCheckResult(used=0, max_tokens=None, exceeded=False)

    async def reset(self, conversation_id: "ConversationId") -> None:
        return None

    async def set_max_tokens(
        self, conversation_id: "ConversationId", max_tokens: int | None
    ) -> "BudgetCheckResult":
        from qai.chat.domain.budget import BudgetCheckResult

        return BudgetCheckResult(used=0, max_tokens=None, exceeded=False)


def _is_anthropic_family(model_id: str | None) -> bool:
    """Return True when ``model_id`` is a Claude / Anthropic-family model.

    Used by the running full-history token counter to decide whether to add
    ``last_round_cache_read_tokens`` back into ``eff_prompt``: Anthropic/Claude
    split cache reads OUT of ``prompt_tokens`` (so the wire size is
    ``prompt_tokens + cache_read_tokens``), whereas OpenAI / Azure / Gemini /
    Vertex already fold cache into ``prompt_tokens``.

    We key on the MODEL ID (``"claude" in model_id``), not the client-supplied
    ``model_provider`` — the provider field is unvalidated client input, while
    the model id is the authoritative selector. Simple substring match is
    reliable here: all Anthropic chat models carry "claude" in the id
    (claude-3-5-sonnet, claude-opus-4, claude-sonnet-4-5, ...).
    """
    return isinstance(model_id, str) and "claude" in model_id.lower()


def _model_hint_prefers_apply_patch(model_hint: str | None) -> bool:
    """Return True when the cloud model should be advertised ``apply_patch``.

    Tools-JSON体积压缩 (A1): the system
    advertises ``apply_patch`` ONLY to GPT/OpenAI-family models and ``edit`` to
    everything else — the two are MUTUALLY EXCLUSIVE on the wire, so a
    Claude/Gemini turn never pays for both schemas (~1.4 KB / ~360 tok saved).

    We key on the MODEL HINT substring (authoritative selector, same pattern as
    :func:`_is_anthropic_family`): a hint mentioning ``gpt`` / ``openai`` /
    ``o1`` / ``o3`` is treated as GPT-family and keeps ``apply_patch``. The
    ``gpt-4`` / ``oss`` carve-outs are excluded (those variants do
    NOT prefer apply_patch).

    CONSERVATIVE fallback: when ``model_hint`` is missing / unrecognised we
    return ``True`` (keep the historical "advertise apply_patch" behaviour) so a
    detection miss NEVER strips a tool the model might rely on — and ``edit`` is
    always kept regardless, so this gate can only ever remove ``apply_patch``.
    """
    if not isinstance(model_hint, str) or not model_hint.strip():
        # Unknown model → keep current behaviour (advertise apply_patch).
        return True
    h = model_hint.lower()
    is_gpt_family = "openai" in h or "o1" in h or "o3" in h or (
        "gpt-" in h and "oss" not in h and "gpt-4" not in h
    )
    return is_gpt_family


# Conditional tools — advertised ONLY in their owning mode, never in the
# default / local tool set. V1 parity: ``backend/tools/_appbuilder_run.py:609``
# + ``_appbuilder_batch_run.py:356`` register with ``conditional=True`` so
# ``registry.schemas(exclude_conditional=True)`` (used by the local path,
# ``local_prompt_builder.py:46``) omits them; they are injected as
# ``extra_tools`` only for cloud + app-builder turns
# (``chat_handler.py:402-410``). The set now lives in the SHARED
# ``tool_advertise`` helper (single source of truth — previously duplicated
# here and in ``agent_tool``); imported above with its historical private alias.


def _agent_tool_schema() -> dict[str, Any]:
    """OpenAI JSON schema for the ``agent`` (sub-agent spawn) tool.

    Byte-for-byte V1 parity with ``backend/chat_handler.py:3319-3348
    _build_agent_tool_schema``. V1 injects this as an ``extra_tools`` entry
    whenever a ``tool_executor`` is present (``chat_handler.py:392-395``), i.e.
    for the depth-0 main agent of every turn — including local-model turns
    (the on-device payload in the live audit carried exactly this 9th tool).

    V2 enhancement (tail-only added, §3.1): the optional ``resume_subagent_id``
    property lets the main agent WAKE a sub-agent it spawned earlier — passing
    the ``subagent_id`` returned by that sub-agent's previous ``subagent_done``
    so it continues WITH its prior messages and tool outputs for a related
    follow-up task, instead of starting a fresh one. ``required`` stays
    ``["prompt"]`` so the contract is unchanged for callers that ignore it.

    V2 enhancement (tail-only added, §3.1): the optional ``subagent_type``
    property lets the model self-select the sub-agent *profile* — ``general``
    (default: full tool set, multi-step agentic loop, may change files / run
    commands) or ``explore`` (strictly READ-ONLY codebase search: only
    read/glob/grep/webfetch, no write/edit/exec). Omitting it (or passing
    ``general``) keeps the historical behaviour, so the contract is unchanged
    for callers that ignore it.
    """
    return {
        "type": "function",
        "function": {
            "name": "agent",
            "description": (
                "Spawn a sub-agent to complete a self-contained task "
                "autonomously. The sub-agent has access to all tools (read, "
                "write, edit, exec, webfetch, glob, grep) and runs its own "
                "agentic loop until the task is done. Use this to delegate "
                "complex subtasks or parallelize work. Returns the "
                "sub-agent's final result as a string, prefixed with a "
                "'subagent_id: <id>' line you can reuse to wake the SAME "
                "sub-agent later for related follow-up work. IMPORTANT: when "
                "the user refers to a sub-agent you created earlier (e.g. "
                "'the sub-agent', 'the first agent', 'ask it again'), do NOT "
                "spawn a new one — reuse that sub-agent's 'subagent_id' as "
                "'resume_subagent_id' below to CONTINUE it. If you spawned it "
                "this turn (or recently) its id is already in the prior "
                "result line, so just reuse it directly; only if you no "
                "longer have the id (it scrolled out of context) call "
                "'list_subagents' to look it up first."
            ),
            "parameters": {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "name": {
                        "description": (
                            "Optional short 3-5 word label shown as the "
                            "sub-agent's UI card title (e.g. 'Fix login bug'). "
                            "Omit for a generic 'SubAgent N' label."
                        ),
                        "type": "string",
                    },
                    "prompt": {
                        "description": (
                            "Complete, self-contained task description — a "
                            "fresh sub-agent shares no memory with you, so "
                            "include all context it needs."
                        ),
                        "type": "string",
                    },
                    "resume_subagent_id": {
                        "description": (
                            "Optional. Pass a 'subagent_id' returned earlier to "
                            "CONTINUE that sub-agent (with its prior context) "
                            "instead of starting a new one."
                        ),
                        "type": "string",
                    },
                    "subagent_type": {
                        "description": (
                            "Optional. 'general' (default) = full tool set, may "
                            "change files / run commands; 'explore' = strictly "
                            "READ-ONLY search (read/glob/grep/webfetch only)."
                        ),
                        "type": "string",
                        "enum": ["general", "explore"],
                    },
                },
            },
            "strict": False,
        },
    }


def _schema_tool_name(schema: dict[str, Any]) -> str:
    """Extract the tool name from an OpenAI function schema (best-effort).

    Thin wrapper over the SHARED ``tool_advertise.schema_tool_name`` (single
    source of truth) that preserves this module's historical ``-> str``
    contract (returns ``""`` rather than ``None`` for a nameless schema) so
    external callers / tests importing ``_schema_tool_name`` are unaffected.
    """
    return _shared_schema_tool_name(schema) or ""


def _session_disabled_tools(extra: dict[str, Any] | None) -> frozenset[str]:
    """Normalise the per-session ("this conversation only") disabled tool set.

    Reads ``extra["disabled_tools"]`` — the additive payload field the
    SessionToolsPopover writes (see ``ChatTab.sessionToolOverride``) — and
    returns the set of tool names the user switched OFF for this conversation.
    Tolerant of missing / malformed values (returns an empty set). This is the
    SINGLE source of truth for the per-session tool block so the advertise
    filter (``_collect_tool_schemas``), the main-agent execution gate
    (``_execute_single_tool_call``), and the sub-agent path all agree.
    """
    raw = (extra or {}).get("disabled_tools")
    if isinstance(raw, (list, tuple)):
        return frozenset(str(n) for n in raw if n)
    return frozenset()


def _persona_disabled_tools(extra: dict[str, Any] | None) -> frozenset[str]:
    """Derive disabled tools from the resolved code persona's groups.

    When a code persona is resolved (``extra["persona_groups"]`` is set),
    any tool whose group is NOT in the persona's allowed groups is
    disabled — hard tool-level isolation.  Returns an empty set when no
    persona groups are active (meaning all tools remain available).

    This is merged (unioned) with ``_session_disabled_tools`` so both
    user-initiated and persona-mandated restrictions apply simultaneously.
    """
    raw = (extra or {}).get("persona_groups")
    if raw is None:
        return frozenset()
    if not isinstance(raw, (list, tuple)):
        return frozenset()
    from qai.chat.domain.tool_groups import groups_to_disabled_tools

    return groups_to_disabled_tools(list(raw))


def _apply_cloud_tool_description_overrides(
    schemas: list[dict[str, Any]],
    overrides: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a NEW schema list with cloud-enhanced descriptions applied.

    Never mutates the input or the shared registry: every nested dict
    touched is rebuilt fresh. On-device turns never call this (they keep
    the short registered text).
    """
    if not overrides:
        return schemas
    out: list[dict[str, Any]] = []
    for schema in schemas:
        fn = schema.get("function") if isinstance(schema, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else None
        override = overrides.get(name) if name else None
        if not override:
            out.append(schema)
            continue
        new_fn = {**fn}
        if override.get("description"):
            new_fn["description"] = override["description"]
        param_descs = override.get("param_descriptions")
        if param_descs:
            params = new_fn.get("parameters")
            if isinstance(params, dict):
                new_params = {**params}
                props = new_params.get("properties")
                if isinstance(props, dict):
                    new_props = {**props}
                    for field, desc in param_descs.items():
                        if field in new_props and isinstance(new_props[field], dict):
                            new_props[field] = {**new_props[field], "description": desc}
                    new_params["properties"] = new_props
                new_fn["parameters"] = new_params
        out.append({**schema, "function": new_fn})
    return out


def _takeover_turn_wire_blocks(
    state: "_TurnBodyState",
) -> list[dict[str, Any]]:
    """Reconstruct a take-over turn's tool rounds as OpenAI wire blocks.

    Returns the ``assistant{tool_calls}`` + paired ``role:tool`` reply blocks
    for every tool round the take-over turn executed, in round order — the same
    completeness口径 a sub-agent's autonomous ``record_round`` persists. Used by
    :meth:`StreamChatUseCase._persist_subagent_takeover` so the sub-agent
    session wire retains the take-over turn's tools (not just its text).

    Faithful to ``build_tool_call_message`` (``_streaming_helpers.py``):
      * rounds are grouped by the ``round_index`` the use case stamps on every
        TOOL_CALL frame (first-seen order);
      * each ``TOOL_RESULT`` frame is paired back to its call by
        ``tool_call_id`` (the authoritative key, robust to out-of-order /
        parallel completion);
      * the per-round ``assistant.tool_calls`` block + ``role:tool`` replies are
        rendered by the SHARED kernel builders (``include_name=True`` — the
        sub-agent口径), so the persisted wire is byte-compatible with both the
        autonomous-run wire and the ``_wire_to_messages`` reload path.

    Returns ``[]`` when the turn issued no tool calls (a pure-text take-over —
    the caller appends only the user + assistant text in that case).
    """
    tc_frames = state.tc_frames
    if not tc_frames:
        return []
    # Pair TOOL_RESULT → result text by tool_call_id (id pairing, not position).
    result_by_id: dict[str, Any] = {}
    for tr_f in state.tr_frames:
        tcid = tr_f.payload.get("tool_call_id")
        if isinstance(tcid, str) and tcid:
            result_by_id[tcid] = tr_f.payload.get("result")
    # Group call frames into rounds by ``round_index`` (first-seen order). When
    # absent on every frame (legacy emitters) fall back to a single round so we
    # never drop tools — the wire stays valid, only the round split coarsens.
    rounds: list[list[StreamFrame]] = []
    round_by_index: dict[int, list[StreamFrame]] = {}
    fallback_round: list[StreamFrame] = []
    for tc_f in tc_frames:
        ri = tc_f.payload.get("round_index")
        if isinstance(ri, int) and not isinstance(ri, bool):
            bucket = round_by_index.get(ri)
            if bucket is None:
                bucket = []
                round_by_index[ri] = bucket
                rounds.append(bucket)
            bucket.append(tc_f)
        else:
            if not fallback_round:
                rounds.append(fallback_round)
            fallback_round.append(tc_f)

    blocks: list[dict[str, Any]] = []
    for round_frames in rounds:
        # Build (tool_name, arguments, call_id) metas in original call order,
        # plus a ``call_id -> thought_signature`` map. The Vertex AI
        # ``thought_signature`` rides on the originating TOOL_CALL frame's
        # payload (``StreamFrame.tool_call`` carries it, §3.1 tail-appended);
        # re-attaching it to the rebuilt ``assistant.tool_calls[i]`` keeps a
        # take-over turn's tool round signature-complete — the SAME口径 the
        # autonomous follow-up loop uses (``_append_tool_round`` /
        # ``streaming.py`` `_record_followup_round`). Without it a later Vertex
        # resume of a taken-over session would hit the lossy
        # ``flatten_tool_calls_without_signature`` fallback (the round's
        # structured tool cards degrade to a plain-text summary). Absent for
        # non-Vertex providers → an empty map → block builder skips signatures.
        tool_metas: list[tuple[str, dict[str, Any], str]] = []
        thought_signatures: dict[str, Any] = {}
        for tc_f in round_frames:
            name = tc_f.payload.get("tool_name") or ""
            args = tc_f.payload.get("arguments")
            call_id = tc_f.payload.get("tool_call_id") or ""
            tool_metas.append(
                (
                    str(name),
                    args if isinstance(args, dict) else {},
                    str(call_id),
                )
            )
            sig = tc_f.payload.get("thought_signature")
            if sig and call_id:
                thought_signatures[str(call_id)] = sig
        if not tool_metas:
            continue
        # Lead-in narration text the model streamed before this round's tools
        # (only the round's first frame carries it) → the assistant content, so
        # a reload renders the same lead-in → tools order.
        lead_in = round_frames[0].payload.get("lead_in")
        lead_in_text = lead_in if (isinstance(lead_in, str) and lead_in.strip()) else ""
        assistant_block: dict[str, Any] = {
            "role": "assistant",
            "content": lead_in_text or TOOL_CALLS_CONTENT_SENTINEL,
            "tool_calls": _kernel_assistant_tool_calls_block(
                tool_metas, thought_signatures=thought_signatures
            ),
        }
        blocks.append(assistant_block)
        ordered_results = [
            result_by_id.get(cid, "") for (_n, _a, cid) in tool_metas
        ]
        blocks.extend(
            _kernel_tool_reply_blocks(
                tool_metas, ordered_results, include_name=True
            )
        )
    return blocks



# ---------------------------------------------------------------------------
# Concrete (default) StreamAbortHandle implementation
# ---------------------------------------------------------------------------
class AsyncioStreamAbortHandle:
    """Default :class:`StreamAbortHandle` backed by ``asyncio.Event``.

    Adapters may swap this for a richer implementation (e.g. one that
    also kills a tools subprocess); the use case only relies on the
    Protocol.

    The ``retry_now`` flag is a SECOND, independent signal (NOT abort): while
    the turn waits out a network-retry backoff, the user may click "立即重试"
    after manually restoring connectivity. Setting it makes the abortable sleep
    return early so the retry loop re-opens the LLM stream at once, WITHOUT the
    abort semantics (the turn continues rather than being torn down). It is
    consumed (auto-cleared) once observed so a single click skips exactly one
    wait.

    The ``cancel_tool`` set is a THIRD, independent signal (also NOT abort):
    the user may cancel ONE running tool call (by ``call_id``) from its tool
    card. The dispatcher polls ``consume_cancel_tool(call_id)`` per in-flight
    tool; a hit cancels just that tool (synthesizing a ``[cancelled]``
    tool_result) while the round keeps draining the others and the turn
    continues — deliberately never tripping :meth:`is_set`.
    """

    __slots__ = ("_event", "_reason", "_retry_now", "_cancel_tools")

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: str | None = None
        self._retry_now = asyncio.Event()
        self._cancel_tools: set[str] = set()

    def signal(self, *, reason: str = "user_requested") -> None:
        if not self._event.is_set():
            self._reason = reason
            self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def request_retry_now(self) -> None:
        """Ask the current network-retry backoff to stop waiting and re-open."""
        self._retry_now.set()

    def consume_retry_now(self) -> bool:
        """Return ``True`` (and clear) iff a retry-now was requested."""
        if self._retry_now.is_set():
            self._retry_now.clear()
            return True
        return False

    def request_cancel_tool(self, call_id: str) -> None:
        """Mark ONE in-flight tool call (by ``call_id``) for cancellation.

        Independent of :meth:`signal` — does NOT trip :meth:`is_set`, so the
        turn continues. A blank ``call_id`` is ignored (nothing to key on).
        """
        cid = (call_id or "").strip()
        if cid:
            self._cancel_tools.add(cid)

    def consume_cancel_tool(self, call_id: str) -> bool:
        """Return ``True`` (and clear) iff ``call_id`` was cancel-requested."""
        cid = (call_id or "").strip()
        if cid and cid in self._cancel_tools:
            self._cancel_tools.discard(cid)
            return True
        return False

    @property
    def reason(self) -> str | None:
        return self._reason


# ---------------------------------------------------------------------------
# StreamChat
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class StreamChatInput:
    """Inputs for :class:`StreamChatUseCase.execute`."""

    tab_id: TabId
    conversation_id: ConversationId
    user_message: MessageContent
    model_hint: str | None = None
    extra: dict[str, Any] | None = None


@dataclass(slots=True)
class _FollowupRoundOutcome:
    """Mutable control-flow result of one follow-up-round stream drain.

    :meth:`StreamChatUseCase._drain_followup_round` is an async generator
    (it ``yield``s frames) so it cannot also ``return`` a value; this
    holder carries the per-round outcome back to :meth:`_run_followup_loop`
    without changing the byte-for-byte yield order:

    * ``seq`` — the advanced sequence counter after the round;
    * ``aborted`` — the abort handle fired mid-round (caller returns);
    * ``terminated`` — a normal END frame was consumed (caller returns);
    * ``retry_empty_completion`` — the empty-completion guard tripped;
      the caller stays in the loop for another round;
    * ``stream_ended`` — an END frame was seen (parity with the legacy
      ``stream_ended`` flag used by the "no pending + not ended" guard);
    * ``compressed_history_override`` — the synthetic-retry history built
      when ``retry_empty_completion`` is set (else unchanged).
    """

    seq: int
    aborted: bool = False
    terminated: bool = False
    retry_empty_completion: bool = False
    stream_ended: bool = False
    compressed_history_override: tuple[Any, ...] | None = None
    # The visible assistant text accumulated during this round's stream.
    # V1 parity (chat_handler.py:775/791-795): the assistant message that
    # carries a round's ``tool_calls`` also carries that round's lead-in
    # ``content``.  Captured here so the next round can append the proper
    # ``assistant{content, tool_calls}`` entry to the growing wire history.
    assistant_text: str = ""


@dataclass(slots=True)
class _CompletedRound:
    """One agentic-loop round's wire block + provider-measured prompt size.

    Replaces the previous anonymous ``(text, tool_calls, tool_results)``
    tuple used in :meth:`StreamChatUseCase._run_followup_loop`. Adds the
    fields needed by the "real-token differential" compression path
    (``docs/90-refactor/CONTEXT-COMPRESSION.md``):

    * ``text`` / ``tool_calls`` / ``tool_results`` — the round's assistant
      lead-in text, tool-call frames it issued, and tool-result dicts (the
      legacy tuple shape, preserved verbatim so the compaction-replay loop
      can rebuild the wire unchanged).
    * ``real_prompt_tokens`` — the provider-measured (or single-round
      tokenizer-fallback) prompt-token size of the wire prefix THIS round
      saw as input. ``0`` means "unmeasured" — the differential path falls
      back to char × density for such rounds.
    * ``completion_tokens`` — the round's completion size from the same
      usage block (kept for diagnostic logging / audit).
    * ``source`` — ``"cloud"`` (provider returned usage), ``"tokenizer"``
      (round-incremental tiktoken fallback, no usage), or ``"unknown"``
      (neither available — ``real_prompt_tokens`` is 0).

    State-Truth-First (AGENTS.md 铁律 1): ``real_prompt_tokens`` is filled
    from the provider's measurement of the actual wire (or a bounded
    tokenizer pass on JUST this round's incremental content); NEVER from a
    char-density estimate at compression time.
    """

    text: str
    tool_calls: list  # type: ignore[type-arg]
    tool_results: list[dict[str, Any]]
    real_prompt_tokens: int = 0
    completion_tokens: int = 0
    source: str = "unknown"


@dataclass(slots=True)
class _TurnTailOutcome:
    """Mutable counters + result carried out of the turn-tail helpers.

    The post-loop finalize helpers of :meth:`StreamChatUseCase._run`
    (:meth:`_finalize_assistant_message`, :meth:`_emit_turn_warning`)
    are async generators (they ``yield`` frames) so they cannot also
    ``return`` the updated ``frame_count`` / ``synth_seq`` counters.
    This holder threads those counters — and the finalised
    ``assistant_msg`` — back to ``_run`` so the byte-for-byte sequence /
    frame-count accounting is unchanged after the split.
    """

    frame_count: int
    synth_seq: int
    assistant_msg: "Message | None" = None


@dataclass(slots=True)
class _TurnBodyState:
    """Mutable accumulator threaded through the main streaming loop.

    The central ``async for frame in stream`` body of
    :meth:`StreamChatUseCase._run` accumulates a dozen pieces of turn
    state (assistant text, tool-call / tool-result frames for
    persistence, usage, sub-agent fold blocks, TTFT, counters, abort
    flags).  :meth:`_drain_main_stream` (B1 cohesion split) is an async
    generator, so it mutates this shared holder in place instead of
    juggling a dozen return values — preserving the original behaviour
    byte-for-byte while letting ``_run`` read the finalised state after
    the drain.

    ``assistant_text_parts`` / ``tc_frames`` / ``tr_frames`` /
    ``sub_agent_blocks`` are shared-by-reference lists/dicts (the
    follow-up loop appends to ``assistant_text_parts`` directly), so they
    are passed by identity and never reassigned.
    """

    assistant_text_parts: list[str]
    tc_frames: list[StreamFrame]
    tr_frames: list[StreamFrame]
    sub_agent_blocks: dict[Any, dict[str, Any]]
    #: Per-round answer text for a SELF-CONTAINED agent (``query::*`` — MB Pro).
    #: Such an agent interleaves text and tool cards freely; the adapter stamps
    #: a distinct ``round_index`` on each contiguous text run and each tool, and
    #: the live frontend renders one message per round in arrival order. To make
    #: a RELOAD match that live order, we capture each CHUNK's text under its
    #: ``round_index`` here so ``build_tool_call_message`` can (a) use a tool
    #: round's own text as that round's lead-in and (b) emit the text-only rounds
    #: (text that streamed between / after tools) as their own assistant messages
    #: at the right position — instead of lumping ALL text into one final message
    #: that reloads AFTER every tool card (the reported "text all at the end"
    #: bug). Empty for ordinary cloud/local turns (their text has no per-round
    #: key and flows through ``assistant_text_parts`` exactly as before).
    chunk_text_by_round: dict[int, list[str]] = field(default_factory=dict)
    #: Per-round prompt-snapshot ids — ``round_index → request_id`` (V1
    #: parity: each agentic LLM round persists its OWN snapshot of the
    #: exact messages sent that round and the messages it produced carry
    #: that round's ``request_id``, so every tool card's 📄 button opens
    #: the prompt of ITS round — different rounds show different prompts
    #: (V1 ``useChat.js`` per-``/api/chat`` ``request_id``). Round 0 = the
    #: initial LLM call; round N = the Nth follow-up stream. Shared by
    #: reference with :meth:`_run_followup_loop` (it writes rounds 1..N).
    round_request_ids: dict[int, str] = field(default_factory=dict)
    turn_usage: dict[str, Any] | None = None
    #: Usage payload of the LAST agentic round of this turn (the true wire size
    #: of that turn). ``turn_usage`` is the SUM across all rounds, so for
    #: multi-round turns its ``prompt_tokens`` is NOT the wire size — the
    #: running full-history counter needs the last round's prompt separately.
    #: Single-round turns: equals ``turn_usage``. Tail-appended (AGENTS.md
    #: §3.1).
    last_round_usage: dict[str, Any] | None = None
    #: Usage payload of the FIRST agentic round (round 0) of this turn. Its
    #: ``prompt_tokens`` (``_extract_usage``-corrected) is the round-0 wire
    #: prompt — the only token count whose round matches ``ttft_ms`` (round-0
    #: prefill latency). The per-message UI "input tok/sec" badge needs both
    #: numerator (round-0 prompt) and denominator (round-0 ttft) from the SAME
    #: round; ``turn_usage.prompt_tokens`` (cross-round SUM) and
    #: ``last_round_prompt_tokens`` (round-N) both mismatch ``ttft_ms``.
    #: Tail-appended to the persisted usage as ``first_round_prompt_tokens``
    #: (AGENTS.md §3.1). Single-round turns: equals ``last_round_usage``.
    first_round_usage: dict[str, Any] | None = None
    ttft_ms: int | None = None
    frame_count: int = 0
    synth_seq: int = 1_000_000
    aborted: bool = False
    abort_reason: str = "user_requested"
    #: Set when ``_run`` caught a non-abort exception mid-loop. Used so the
    #: error path can flush already-completed tool rounds before re-raising
    #: (V1 ``finally``-save parity).
    run_error: BaseException | None = None
    #: Number of ``conv.messages`` that exist BEFORE this turn appended any of
    #: its own assistant / tool-round messages (index just past the freshly
    #: appended user message). Incremental per-round persistence
    #: (:meth:`StreamChatUseCase._persist_completed_rounds`) truncates
    #: ``conv.messages`` back to this boundary and rebuilds the turn's tool
    #: messages from ``tc_frames`` / ``tr_frames`` each round, so repeated
    #: saves stay idempotent (no duplicate stacking) and a crash / kill that
    #: never runs an ``except`` block still leaves every completed round on
    #: disk. ``-1`` = not yet recorded (set in ``_run`` after the user message
    #: is appended).
    turn_persist_baseline: int = -1
    #: Set when the LLM stream already emitted an ERROR frame (e.g.
    #: ``chat.llm.connect_error`` / ``chat.llm.timeout``).  Prevents
    #: ``_finalize_turn`` from emitting a redundant ``empty_response``
    #: that overwrites the real diagnostic on the frontend.
    had_llm_error: bool = False
    #: Mid-turn user injections (V2 enhancement) folded into THIS turn via the
    #: inject button, in arrival order. Each entry is the minted ``role:user``
    #: :class:`Message` plus the 0-based agentic ``round_no`` it landed before.
    #: The turn's history is REBUILT from frames each persist (the truncate +
    #: ``build_tool_call_message`` rebuild drops everything past the baseline),
    #: so an injected user message appended straight to ``conv`` would be lost
    #: on the next rebuild. Instead the run loop records them here and BOTH
    #: persist sites (:meth:`_persist_completed_rounds` / ``_finalize_turn``)
    #: re-insert them after rebuilding, positioned right after the round they
    #: followed — so the persisted (reload) order matches the live (frame)
    #: order. Tuple shape: ``(round_no, Message)``.
    injected_messages: list[tuple[int, Message]] = field(default_factory=list)
    #: LEAK FIX (2026-07-11): True when this turn is a user TAKE-OVER of a
    #: sub-agent (``request.extra["subagent_id"]`` set). Its transcript is
    #: persisted ONLY into ``SubAgentSession`` (:meth:`_persist_subagent_takeover`),
    #: so every parent-conversation ``save_messages`` in this turn — normal,
    #: interrupt and error tails — must be suppressed via
    #: :meth:`_save_parent_conv` to stop the sub-agent's last message (incl. its
    #: ``question``) leaking into the MAIN agent's history on reload. Threaded on
    #: ``state`` (per-turn) because several persistence helpers do not receive
    #: ``request``.
    is_subagent_takeover: bool = False


class StreamChatUseCase:
    """Stream a single chat turn for a given (tab, conversation) pair.

    Lifecycle (happy path)::

        1. Load conversation aggregate
        2. Load tab; transition IDLE -> STREAMING
        3. Append user message; emit MessageAppendedEvent
        4. Register abort handle in registry (raises if duplicate)
        5. Emit ChatStreamStartedEvent
        6. Open LLM stream (with retry on prompt-too-long / throttling
           when ``retry_policy`` is wired — PR-401c)
        7. async for frame in llm.stream(...):
              - emit ChatStreamFrameEvent
              - if abort_handle.is_set(): raise ChatStreamAbortedError
              - if frame is TOOL_CALL and the agentic loop is enabled
                (``max_followup_rounds > 0`` AND ``guardrail_factory`` AND
                ``tool_result_truncator`` injected): execute the tool
                inline, emit a TOOL_RESULT frame, accumulate the
                turn, and feed the tool result back into a follow-up
                LLM stream (up to ``max_followup_rounds`` rounds).
              - yield frame to caller
        8. Append assistant message; emit MessageAppendedEvent
        9. Tab transitions STREAMING -> IDLE
        10. Emit ChatStreamCompletedEvent
        11. Unregister abort handle (always, in finally)

    On user abort:

        - ``StopChatUseCase`` calls ``registry.abort(tab_id)``.
        - The handle's event is set; this iterator detects it on the
          next frame, raises :class:`ChatStreamAbortedError` internally,
          marks the tab ABORTED, emits :class:`ChatStreamAbortedEvent`,
          yields a final ERROR frame, then exits cleanly.

    PR-401c agentic-loop wiring (all five params are optional and
    default to disabled — legacy callers see byte-for-byte PR-033
    behaviour):

    * ``retry_policy`` — when supplied, the use case looks at the
      first ERROR frame from the LLM stream and consults the policy
      to decide whether to retry (with backoff for throttling, with
      compression hint for prompt_too_long).  See
      :class:`qai.chat.application.ports.RetryPolicyPort`.
    * ``guardrail_factory`` — when supplied alongside
      ``tool_result_truncator``, every in-stream tool call goes
      through ``GuardrailPort.check`` before invocation and
      ``GuardrailPort.observe`` after.  ``BLOCK`` short-circuits
      execution with a synthetic ``[guardrail_blocked]`` result.
    * ``tool_result_truncator`` — applied to the raw tool output
      before it is fed back to the LLM in the next follow-up round.
    * ``system_prompt_builder`` — currently informational; PR-401c
      reserves the slot so PR-401d can pre-pend a system prompt
      strategy without re-writing the constructor.
    * ``max_followup_rounds`` — upper bound on follow-up LLM rounds
      for the agentic loop.  ``0`` (default) disables the loop and
      restores PR-033 behaviour: tool calls are forwarded through
      :meth:`_dispatch_tool` (best-effort, no follow-up).
    """

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        tabs: TabSessionStorePort,
        llm: LLMStreamPort,
        tools: ToolInvocationPort,
        abort_registry: StreamAbortRegistryPort,
        clock: Clock,
        ids: IdGenerator,
        events: EventBus | None = None,
        abort_handle_factory: type[AsyncioStreamAbortHandle] = AsyncioStreamAbortHandle,
        # ---- PR-401c agentic-loop ports (all optional; default = disabled) ----
        retry_policy: RetryPolicyPort | None = None,
        guardrail_factory: Callable[[], GuardrailPort] | None = None,
        tool_result_truncator: ToolResultTruncatorPort | None = None,
        system_prompt_builder: SystemPromptBuilderPort | None = None,
        context_compressor: ContextCompressionPort | None = None,
        experience_extractor: Any | None = None,
        prompt_snapshot_store: PromptSnapshotStorePort | None = None,
        experience_recall: ExperienceRecallPort | None = None,
        # ---- PR-subagent-stream sub-agent SSE port (V1 parity) ----
        # When wired AND a follow-up round encounters an ``agent``
        # ``tool_call`` frame, the use case dispatches via this port's
        # ``iter_events`` instead of ``ToolInvocationPort.invoke`` and
        # forwards the structured ``subagent_*`` events to the parent
        # stream as :data:`StreamFrameType.SUBAGENT_*` frames (V1
        # ``backend/chat_handler.py:2188-2343`` user-visible parity).
        # When ``None`` the legacy collapsed-string path is used (the
        # registered ``agent`` tool's ``execute`` callback returns the
        # consolidated text only) so the use case stays backward
        # compatible with deployments that do not wire the new port.
        agent_event_stream: SubAgentEventStreamPort | None = None,
        # ---- R12 dealign: cross-BC code-persona resolver (apps bridge) ----
        # When wired AND a turn carries ``tool_mode == "code"`` with a
        # ``tool_params.persona`` id, the use case resolves the persona
        # (id → prompt + display name) through this port and merges it
        # into ``extra["persona"]`` / ``extra["persona_name"]`` so the
        # system-prompt builder injects it as the active working role.
        # The resolution used to live in the SSE / WS route layer
        # (``_resolve_code_persona_into_extra``) which imported
        # ``qai.user_prefs`` directly; R12 moves it behind this port +
        # the ``apps/api`` bridge so the chat context never imports
        # user_prefs.  ``None`` disables persona injection.
        code_persona_resolver: CodePersonaResolverPort | None = None,
        max_followup_rounds: int = DEFAULT_MAX_FOLLOWUP_ROUNDS,
        sleep: Callable[[float], Any] | None = None,
        # ---- Operator hooks (migrated from ai_coding agent harness) ----
        # When wired, runs configured shell commands at agent-loop
        # lifecycle points (session start/end, pre/post message, pre/post
        # tool call, error, truncate).  ``None`` disables hooks (zero
        # cost).  The apps layer builds the engine from forge_config.
        hook_engine: HookEnginePort | None = None,
        # ---- Local (on-device) model simplified-prompt path ----
        # V1 parity (``backend/chat_handler.py:414-433`` dispatch +
        # ``_build_local_messages`` 952-1055): when wired AND the turn
        # targets a ``local::*`` model, the use case builds a MINIMAL
        # system prompt (front-end base system + ``agent=main`` +
        # ``<available_skills>`` metadata XML) instead of the full cloud
        # ``RichSystemPromptBuilder`` output.  The provider is a zero-arg
        # callable returning ``((skill_id, use_for, path), ...)`` triples
        # for LOCAL-visible skills (mode in {local, both}); wired via the
        # ``apps/api`` bridge.  When ``None`` the local path still builds the
        # minimal prompt (``agent=main`` + base system) but with an empty
        # ``<available_skills>`` block — it does NOT fall back to the cloud
        # builder (the ``local::`` dispatch is unconditional, V1 parity).
        local_skill_catalog_provider: (
            Callable[[], tuple[tuple[str, str, str], ...]] | None
        ) = None,
        # ---- Sub-agent session store (user take-over of a sub-agent) ----
        # When wired AND a turn carries ``extra["subagent_id"]``, the turn is
        # a USER TAKE-OVER: the use case loads that sub-agent's persisted wire
        # history as the turn's base context (instead of the parent
        # conversation), restricts to the sub-agent tool set (no nested
        # ``agent``), and persists the user's turn back onto the same
        # sub-agent session. ``None`` disables take-over (the ``subagent_id``
        # extra is then ignored — the turn behaves as a normal conversation
        # turn). Shared-ownership model: the main agent can still later wake
        # the same sub-agent to read the user-driven conclusion.
        sub_agent_sessions: SubAgentSessionRepositoryPort | None = None,
        # ---- Runtime debug-config reader (forge-config service_launch) ----
        # Optional zero-arg callable (sync or async) returning the per-turn
        # runtime debug flags, e.g. ``{"prompt_debug": bool,
        # "show_prompt_in_ui": bool}``.  Read ONCE at the start of every turn
        # so an operator's forge-config edit takes effect on the next message
        # WITHOUT a restart (the apps bridge re-reads the on-disk
        # ``forge_config.json`` each call).  ``None`` (or any missing key)
        # restores the prior behaviour: ``prompt_debug`` defaults to False
        # (no console dump) and ``show_prompt_in_ui`` defaults to True (always
        # save the prompt snapshot).  Wired at the ``apps/api`` composition
        # root (cross-context isolation: the chat BC never imports
        # ``qai.user_prefs``).
        runtime_debug_config: (
            Callable[[], Awaitable[dict[str, Any]] | dict[str, Any]] | None
        ) = None,
        # ---- Compaction ratio provider (user-prefs sliders) ----
        # Optional zero-arg callable (sync or async) returning the per-turn
        # context-compression ratios the user chose in Settings -> App Config ->
        # Agent Loop: ``{"target": float, "protect": float}`` (both 0.0..1.0).
        #   * ``target``  -- post-compression keep size as a fraction of the
        #     model window (the compressor's ``target_window_ratio``); default
        #     0.35 (compress a 200K window down to ~70K).
        #   * ``protect`` -- most-recent history protected verbatim as a
        #     fraction of the window (the compressor's ``protect_ratio``);
        #     default 0.35.
        # Read per compaction (not cached) so a Settings change takes effect on
        # the next compaction WITHOUT a restart. ``None`` (or any missing /
        # malformed key) restores the prior behaviour byte-for-byte: both ratios
        # fall back to the kernel defaults (0.35 / 0.35). Wired at the
        # ``apps/api`` composition root so the chat BC never imports
        # ``qai.user_prefs`` (cross-context isolation, AGENTS.md 3.2).
        compaction_ratio_provider: (
            Callable[[], Awaitable[dict[str, float]] | dict[str, float]] | None
        ) = None,
        # ---- Image upload store (V2 enhancement: question-answer images) ----
        # Optional ``ImageUploadStorePort`` used ONLY to decode images the user
        # attached to a ``question`` tool answer (``![](/api/images/files/..)``)
        # into OpenAI-Vision blocks injected as a follow-up ``role:user``
        # multimodal message, so a vision model can SEE the pixels (tool
        # results are always pinned to plain text). ``None`` disables the
        # injection (the answer's image refs then reach the model only as URL
        # text, the prior behaviour). Wired at the apps composition root from
        # the same ``FileSystemImageUploadStore`` the upload endpoint uses.
        image_upload_store: ImageUploadStorePort | None = None,
        # ---- Mid-turn user injection (V2 enhancement) ----
        # Optional tab-keyed registry the run loop drains at its inter-round
        # seam, folding each pending user injection into the SAME run as a
        # ``role:user`` message (persisted + an ``injected_message`` frame).
        # Shares the SAME instance the route-layer control WS writes to.
        # ``None`` disables the feature (no inter-round injection), so
        # sub-agent / discussion / legacy callers that do not wire it see
        # byte-for-byte unchanged behaviour.
        injection_registry: InjectionRegistryPort | None = None,
        tool_concurrency: ToolConcurrencyManager | None = None,
        # ---- Compaction-checkpoint durable store (CCD-5) ----
        # Optional ``CompactionCheckpointStorePort`` backing the in-process
        # ``_compaction_checkpoints`` dict. When wired, every checkpoint
        # create/update WRITES THROUGH to sqlite and the use case LAZY-LOADS a
        # conversation's checkpoint from sqlite on its first turn after a
        # restart, so compaction state survives a process restart (PENDING-
        # WORK.md §1 CCD-5). ``None`` (default) restores the prior PURE-MEMORY
        # behaviour byte-for-byte: no persistence, checkpoint lost on restart.
        compaction_checkpoint_store: "CompactionCheckpointStorePort | None" = None,
        # ---- 方案B: gateway prompt-cache-support registry ----
        # Optional shared ``ProviderCacheCapabilityRegistry`` that learns at
        # runtime whether the routed gateway echoes Anthropic prompt-cache
        # accounting. The per-round aging gate consults ``aging_enabled`` BEFORE
        # calling ``age_old_tool_outputs``: unknown (first round) / supports-cache
        # → aging OFF (keep the cached prefix byte-clean); no-cache → aging ON
        # (real byte savings). ``_on_round_end`` writes the gateway's cache
        # support back via ``mark`` once it sees the round usage's
        # ``provider_reported_cache`` flag. ``None`` (legacy / unit stubs)
        # disables the gate → aging runs unconditionally, byte-for-byte the prior
        # behaviour (full backward compatibility). The SAME instance is shared
        # with the sub-agent handler + routing stream (DI, ``_chat_di.py``).
        provider_cache_registry: "ProviderCacheCapabilityRegistry | None" = None,
        # ---- Per-conversation token-budget tracker (max_budget_tokens) ----
        # Optional ``BudgetTrackerPort`` gating each agentic round on a
        # per-conversation TOKEN cap (renamed from the CC SDK's
        # ``max_budget_usd`` — no cross-provider USD pricing, but accurate
        # provider usage counts). The loop OBSERVES each round's
        # provider-measured token delta (``effective_prompt_tokens`` +
        # ``completion_tokens``) and CHECKS the pool at each round boundary;
        # once ``used >= max_tokens`` it lets the in-flight round finish (can't
        # un-send a round) but does NOT start another, ending the turn with
        # ``END(reason="budget_exceeded")``. ``None`` (default) → a
        # ``NullBudgetTracker`` is used so budgeting is disabled and every turn
        # is byte-for-byte unchanged (State-Truth-First: local / no-usage rounds
        # are never counted or blocked). Wired at ``_chat_di.py`` behind
        # ``Settings.chat_budget_enabled``; the SAME instance is shared with the
        # sub-agent handler so sub-agents draw from the parent's pool.
        budget_tracker: "BudgetTrackerPort | None" = None,
        # Percentage the cap is raised by when the user chooses "continue" in
        # the frontend's budget-decision dialog (settings ``chat_budget_raise_pct``;
        # default 20 → +20%). Surfaced in the terminal ``END`` payload so the
        # dialog can show the resulting new cap before the user confirms; the
        # frontend applies the raise via ``PATCH .../budget`` then resends a
        # continuation turn (no in-stream suspend — see the budget-decision flow).
        budget_raise_pct: int = 20,
        # ---- Promote-ready turn-end detection (migration 057) ----
        # Optional ``PromoteReadyScanPort`` used at turn end to scan the model
        # workspace path mentioned in the final summary for promote-eligible
        # precision variants; the result is persisted onto
        # ``Conversation.detected_model`` so the frontend "Promote to App
        # Builder" CTA needs ZERO on-open disk scans and refreshes once per
        # turn (replacing the old every-message global scan). ``None`` (default
        # / unit stubs) disables detection — the turn is byte-for-byte
        # unchanged. Wired at ``_chat_di.py`` via the apps-layer
        # ``_promote_ready_scan_bridge`` adapter (chat never imports
        # ``qai.app_builder``; the bridge maps this port onto
        # ``ImportScanBinsUseCase``). Best-effort: any extraction / scan / save
        # failure is swallowed and never breaks turn completion.
        promote_ready_scan: "PromoteReadyScanPort | None" = None,
    ) -> None:
        self._conversations = conversations
        self._tabs = tabs
        self._llm = llm
        self._tools = tools
        self._abort_registry = abort_registry
        self._clock = clock
        self._ids = ids
        self._events = events
        self._abort_handle_factory = abort_handle_factory
        self._budget_raise_pct = max(1, int(budget_raise_pct))
        # Promote-ready turn-end detector (optional; None disables detection).
        self._promote_ready_scan = promote_ready_scan
        # PR-401c
        self._retry_policy = retry_policy
        self._guardrail_factory = guardrail_factory
        self._tool_result_truncator = tool_result_truncator
        self._system_prompt_builder = system_prompt_builder
        self._context_compressor = context_compressor
        # Session-level compaction checkpoints — now owned by a dedicated,
        # conversation-agnostic ``CompactionCheckpointEngine`` (extracted from
        # the former inline ``_compress_via_checkpoint`` so the SAME algorithm
        # can be reused by the sub-agent tool handler). The use case stays the
        # thin conv-aware wrapper: it assembles the wire / derives the anchor /
        # fires the ON_TRUNCATE hook, the engine owns the trigger gate, the
        # real-token attribution, the compressor call, the in-memory cache and
        # the durable write-through store.
        #
        # The engine's in-memory dict is keyed by ``_conv_key(conv)`` (no
        # prefix — this is the only consumer of this engine instance), so the
        # ``_compaction_checkpoints`` / ``_compaction_checkpoint_loaded``
        # properties below proxy DIRECTLY to the engine's containers and every
        # existing sync reader (``_assemble_history_wire`` /
        # ``estimate_compacted_tokens`` / the compact hook) is unchanged.
        # CCD-5: when ``compaction_checkpoint_store`` is wired the engine's dict
        # is a WRITE-THROUGH fast path over durable sqlite (survives restart);
        # ``None`` keeps it pure-memory (prior behaviour, lost on restart).
        self._compaction_checkpoint_store = compaction_checkpoint_store
        self._compaction_engine = CompactionCheckpointEngine(
            compressor=context_compressor,
            ratio_provider=compaction_ratio_provider,
            checkpoint_store=compaction_checkpoint_store,
            threshold_ratio=_INTER_ROUND_COMPRESS_THRESHOLD_RATIO,
            target_ratio=_COMPRESS_TARGET_RATIO,
            preserve_tail=_COMPRESS_PRESERVE_TAIL,
        )
        # PR-091 H-6: optional LLM-powered experience extractor.  Wired
        # by ``apps/api/_chat_di.py``; ``None`` disables auto-extraction.
        self._experience_extractor = experience_extractor
        # W1-F: optional prompt snapshot store for recording the full
        # messages list sent to the LLM (debug capture path).
        self._prompt_snapshot_store = prompt_snapshot_store
        # W1-F: optional experience recall port for injecting memory
        # context (past experiences) into the system prompt.
        self._experience_recall = experience_recall
        # PR-subagent-stream: optional port for streaming sub-agent
        # events when the LLM dispatches the ``agent`` tool.  When
        # ``None`` the use case falls through to the legacy collapsed-
        # string path via ``ToolInvocationPort``.
        self._agent_event_stream = agent_event_stream
        # R12 dealign: optional cross-BC code-persona resolver (apps
        # bridge); ``None`` disables persona injection.
        self._code_persona_resolver = code_persona_resolver
        self._max_followup_rounds = max(0, int(max_followup_rounds))
        self._sleep = sleep if sleep is not None else asyncio.sleep
        # Inter-frame stall ceiling for ``_abortable_frames`` (root-cause 2b):
        # the max wall-clock gap between two LLM stream frames before the turn
        # is treated as a wedged upstream and stopped (so the tab is released
        # instead of pinned "generating" forever when the user does not Stop).
        # Mirrors the sub-agent fan-out merge budget. A healthy stream delivers
        # frames far more often; this is only a failsafe ceiling.
        self._frame_stall_budget_s = 600.0
        # Operator hooks (migrated from ai_coding agent harness); ``None``
        # disables — fire() helper short-circuits so zero cost.
        self._hook_engine = hook_engine
        # Local (on-device) simplified-prompt path provider (V1 parity);
        # ``None`` disables — local turns then use the cloud builder.
        self._local_skill_catalog_provider = local_skill_catalog_provider
        # Sub-agent session store for user take-over (``extra["subagent_id"]``);
        # ``None`` disables take-over.
        self._sub_agent_sessions = sub_agent_sessions
        # Runtime debug-config reader (forge-config service_launch); ``None``
        # disables — prompt_debug then off, show_prompt_in_ui then on (prior
        # behaviour). Read once per turn in ``_run`` so an operator edit is
        # picked up live (the apps bridge re-reads forge_config.json each call).
        self._runtime_debug_config = runtime_debug_config
        # Compaction ratio provider (user-prefs Agent-Loop sliders); ``None``
        # disables -- both ratios then fall back to the kernel defaults
        # (PROTECT_WINDOW_RATIO / COMPRESS_TARGET_WINDOW_RATIO = 0.35 / 0.35),
        # i.e. byte-for-byte the prior behaviour. Read per compaction so a
        # Settings change is picked up live (the apps bridge re-reads the
        # user_prefs forge.config document each call).
        self._compaction_ratio_provider = compaction_ratio_provider
        # Image upload store for decoding question-answer images into vision
        # blocks (V2 enhancement); ``None`` disables the injection.
        self._image_upload_store = image_upload_store
        # Mid-turn user injection registry (V2 enhancement); ``None`` disables
        # the inter-round injection seam (sub-agent / discussion / legacy
        # callers see unchanged behaviour).
        self._injection_registry = injection_registry
        # Shared cross-agent tool concurrency budget (parallel-tool §5): the
        # SAME instance the sub-agent handler draws from, so the round's
        # concurrent non-``agent`` tools (and sub-agents) never storm the
        # machine. None → unbounded (legacy / minimal-container parity).
        self._tool_concurrency = tool_concurrency
        # Per-conversation turn-warning threshold tracker (V1 parity —
        # ``backend/main.py:1326`` ``_webui_turn_warning_thresholds``).
        # Maps ConversationId.value → highest threshold already warned
        # in that conversation, so we never re-warn for the same band.
        # The use case is a singleton (built once in ``apps/api/_chat_di.py``);
        # this dict therefore lives for the process lifetime, matching V1's
        # process-global dict.
        self._turn_warning_thresholds: dict[str, int] = {}
        # Per-conversation MONOTONIC aged-tool lower-bound (改动2b — Anthropic
        # prompt-cache stability). Maps ``_conv_key(conv)`` → the count of the
        # OLDEST ``role:tool`` results that have been aged into placeholders on
        # the SEND wire so far this process, only ever INCREASING. Each round's
        # ``_on_round_open`` feeds this as ``age_old_tool_outputs(...,
        # min_aged_tool_count=N)`` so the low prefix (tools + system + the
        # frozen aged region) is byte-identical across rounds → an Anthropic
        # ``cache_control`` breakpoint just after it actually HITS instead of
        # missing on every sliding-window re-cut. This ONLY influences the
        # per-round SEND-wire copy and the cache breakpoint position — it is
        # NEVER written back to ``wire_messages`` / ``conv.messages`` (the
        # durable full history the UI replays is untouched: hard red-line, no
        #回看 regression). Process-lifetime like ``_turn_warning_thresholds``
        # (the use case is a singleton).
        self._aged_tool_lowerbound: dict[str, int] = {}
        #: Per-conversation cache-breakpoint anchor baseline (改动: 平稳期缓存闭环).
        #: The aged-prefix COUNT used for the Anthropic cache breakpoint is FROZEN
        #: at this baseline between compactions, so the breakpoint position does
        #: NOT drift as the 40K protect window slides (which would grow
        #: ``_aged_oldest_run`` every round and move the breakpoint back → prefix
        #: grows → cache miss). Updated ONLY when a compaction produces a new
        #: checkpoint (the head changes anyway that round, so the breakpoint
        #: SHOULD move then). Monotonic within a compaction epoch is not required —
        #: it is simply held constant until next compaction. Memory-only, never
        #: persisted, never written back to wire_messages / conv.messages.
        self._cache_anchor_baseline: dict[str, int] = {}
        # 方案B: shared gateway prompt-cache-support registry (see __init__ arg
        # doc). ``None`` → aging gate falls back to unconditional aging (prior
        # behaviour). Read in ``_on_round_open`` (gate) + written in
        # ``_on_round_end`` (learn from round usage's ``provider_reported_cache``).
        self._provider_cache_registry = provider_cache_registry
        # Per-conversation token-budget tracker (max_budget_tokens). ``None`` →
        # the ``_budget`` property lazily returns a NullBudgetTracker (disabled).
        self._budget_tracker = budget_tracker
        self._null_budget_tracker: "BudgetTrackerPort | None" = None
        #: Per-conversation "last seen compaction checkpoint signature" (改动:
        #: 平稳期缓存闭环). Maps ``_conv_key(conv)`` → the checkpoint's
        #: ``anchor_index`` observed on the previous round (``None`` when no
        #: checkpoint yet). A change of this signature between rounds means a
        #: compaction produced a NEW checkpoint (the compacted head was replaced),
        #: which is the ONLY moment ``_cache_anchor_baseline`` is re-based. Within
        #: a compaction epoch it stays constant → the cache breakpoint is
        #: byte-stable. Memory-only, never persisted.
        self._last_seen_checkpoint_sig: dict[str, int | None] = {}
        # Shared round-iteration skeleton (§15): the SAME kernel the sub-agent
        # loop drives. The main loop's per-round skeleton (abort check → inter-
        # round compaction → build send wire → open stream → drain & classify
        # → execute tool calls → grow wire → cap) is the kernel's; the main
        # loop's专有 machinery (dual-track compaction checkpoint, per-round
        # prompt snapshot + request_id stamping, empty-completion retry, usage
        # accumulation, experience extraction, no-progress breaker, exec
        # streaming partials, agent recursion dispatch) is injected via the
        # kernel's optional hooks (compact_hook / on_round_open / on_round_end /
        # grow_wire_hook / tool_executor) so the kernel stays conv-agnostic and
        # the main loop's outbound wire + frames stay byte-for-byte identical.
        # compressor/truncator unused by the kernel on this path
        # (the main loop's compaction is its own checkpoint hook), passed for
        # completeness.
        self._kernel = SingleAgentTurnKernel(
            compressor=self._context_compressor,
            truncator=self._tool_result_truncator,
        )

    @property
    def _budget(self) -> "BudgetTrackerPort":
        """Return the wired budget tracker, or a lazy no-op default.

        ``budget_tracker=None`` (the default) yields a cached
        :class:`_NoopBudgetTracker` so every ``check`` returns
        ``exceeded=False`` and ``observe`` is a no-op — budgeting disabled,
        byte-for-byte the prior behaviour. The no-op is a tiny application-layer
        class (NOT the ``qai.chat.adapters`` ``NullBudgetTracker``) so the
        application layer never imports adapters (import-linter layered
        contract). Production DI always injects a real tracker, so this fallback
        only serves legacy / unit-stub callers.
        """
        tracker = self._budget_tracker
        if tracker is not None:
            return tracker
        cached = getattr(self, "_null_budget_tracker", None)
        if cached is None:
            cached = _NoopBudgetTracker()
            self._null_budget_tracker = cached
        return cached

    def _budget_decision_payload(
        self, *, used_tokens: int, max_tokens: int
    ) -> dict[str, int]:
        """Decision metadata appended to the ``budget_exceeded`` terminal END.

        Lets the frontend render the "continue / stop" dialog with the exact
        numbers: current usage, the cap that was hit, and the cap that a
        "continue" would apply (current + ``chat_budget_raise_pct``%). The
        frontend applies the raise via ``PATCH .../budget`` and resends a
        continuation turn (no in-stream suspend). AGENTS.md §3.1 tail-append.
        """
        raise_pct = self._budget_raise_pct
        next_max = max_tokens + (max_tokens * raise_pct) // 100
        if next_max <= max_tokens:  # defensive: always strictly grow
            next_max = max_tokens + 1
        return {
            "budget_used_tokens": int(used_tokens),
            "budget_max_tokens": int(max_tokens),
            "budget_next_max_tokens": int(next_max),
            "budget_raise_pct": int(raise_pct),
        }

    async def _fire_hook(
        self,
        event: HookEvent,
        *,
        payload: dict[str, Any] | None = None,
    ) -> "HookFiredRecord | None":
        """Fire an operator hook for ``event`` if a hook engine is wired.

        Zero cost when no engine is configured (the common case).  Never
        raises: hook failures are logged inside the engine and the turn
        continues (the engine's own ``raise_on_failure`` flag governs
        strict mode, which production wiring leaves off).

        Returns the :class:`HookFiredRecord` (or ``None`` when no engine /
        no hook / a failure) so interceptor-aware call sites (``pre_tool_call``
        / ``pre_message``) can read the hook's steering directives
        (``decision`` / ``updated_input`` / ``additional_context``).
        Observe-only call sites simply ignore the return value, so their
        behaviour is unchanged.
        """
        if self._hook_engine is None:
            return None
        try:
            return await self._hook_engine.fire(event, payload=payload)
        except Exception:  # noqa: BLE001 — hooks must never break a turn
            _log.warning("chat.hook_fire_failed event=%s", event.value, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Runtime debug flags (forge-config service_launch) — read once per turn
    # ------------------------------------------------------------------
    async def _load_runtime_debug_flags(self) -> tuple[bool, bool]:
        """Return ``(prompt_debug, show_prompt_in_ui)`` for this turn.

        Reads the injected :attr:`_runtime_debug_config` callable (sync or
        async). ``None`` / failure / missing keys degrade gracefully to the
        prior behaviour: ``prompt_debug=False`` (no console dump) and
        ``show_prompt_in_ui=True`` (always save the snapshot). The callable is
        invoked once per turn so an operator's forge-config edit is honoured on
        the next message without a restart (the apps bridge re-reads
        ``forge_config.json`` each call).
        """
        if self._runtime_debug_config is None:
            return (False, True)
        try:
            result = self._runtime_debug_config()
            if isinstance(result, Awaitable):
                result = await result
        except Exception as exc:  # noqa: BLE001 — never break a turn on debug cfg
            _log.warning("chat.runtime_debug_config_failed", error=str(exc))
            return (False, True)
        if not isinstance(result, dict):
            return (False, True)
        prompt_debug = bool(result.get("prompt_debug", False))
        # Default True (V1 forge_config.json:51 + ``forge_config.py`` default):
        # absent key means "show" so the prior always-save behaviour holds.
        show_prompt_in_ui = bool(result.get("show_prompt_in_ui", True))
        return (prompt_debug, show_prompt_in_ui)

    def _log_prompt_debug(
        self,
        *,
        channel: str,
        model_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Dump the FULL wire messages to the backend log (V1 prompt_debug).

        Mirrors legacy ``backend/chat_handler.py:3304-3316``
        (``_log_full_messages``): records the complete messages list actually
        sent to the model — each entry's ``role`` + full ``content`` (NOT
        truncated; this is a debug capture) + the key list of any extra fields
        (``tool_calls`` / ``tool_call_id`` / ...).  V1 printed a ``"="*70``
        ASCII block at INFO level; V2 emits ONE structured ``chat.prompt_debug``
        event (structlog parity with the rest of the adapter) so the dump is
        greppable and machine-parseable.  Best-effort: never raises.
        """
        try:
            rows: list[dict[str, Any]] = []
            for i, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                extra_keys = [
                    k for k in msg.keys() if k not in ("role", "content")
                ]
                rows.append(
                    {
                        "index": i,
                        "role": msg.get("role", "?"),
                        # Full content, not truncated (V1 parity — debug only).
                        "content": content if content is not None else "",
                        "extra_fields": extra_keys,
                    }
                )
            _log.info(
                "chat.prompt_debug",
                channel=channel,
                model=model_id,
                message_count=len(rows),
                messages=rows,
            )
        except Exception as exc:  # noqa: BLE001 — debug dump must never break a turn
            _log.warning("chat.prompt_debug_failed", error=str(exc))

    def _build_prompt_debug_messages(
        self,
        *,
        extra: dict[str, Any],
        history: tuple[Any, ...],
        request: "StreamChatInput",
    ) -> list[dict[str, Any]]:
        """Build the full messages list to dump for ``prompt_debug``.

        Returns the OpenAI-wire-shaped messages the adapter is about to send:
        the SYSTEM prompt (resolved the same way the snapshot does) followed by
        the wire body. When ``_build_llm_request`` already assembled an
        ``extra["messages"]`` override (history-rebuild / multimodal / sub-agent
        take-over) we dump that verbatim; otherwise we reconstruct the
        equivalent ``rebuild(history) + current-user`` list the adapter builds
        from ``history`` + ``prompt``, so the dump matches the real wire even on
        the common flat path. Pure / best-effort: never raises (the caller
        wraps the actual logging).
        """
        messages: list[dict[str, Any]] = []
        _system_prompt = self._resolve_snapshot_system_prompt(
            extra=extra, request=request,
        )
        if _system_prompt:
            messages.append({"role": "system", "content": _system_prompt})

        override = extra.get("messages")
        if isinstance(override, list) and override:
            for entry in override:
                if isinstance(entry, dict):
                    messages.append(entry)
            return messages

        # Flat path: the adapter builds ``rebuild(history) + current user``.
        try:
            body = _rebuild_history_wire_messages(history)
        except Exception:  # noqa: BLE001 — best-effort reconstruction
            body = []
        for entry in body:
            if isinstance(entry, dict):
                messages.append(entry)
        messages.append(
            {
                "role": "user",
                "content": getattr(request.user_message, "text", "") or "",
            }
        )
        return messages

    async def execute(
        self,
        request: StreamChatInput,
    ) -> AsyncIterator[StreamFrame]:
        """Return an async iterator over outgoing :class:`StreamFrame` values.

        The use case does not return until the iterator is fully drained
        by the caller; each yielded frame has already been persisted as
        an event before reaching the caller.
        """
        return self._run(request)

    async def collect_completion_text(self, request: StreamChatInput) -> str:
        """Run a turn and return the concatenated assistant text.

        Non-streaming convenience for callers (the OpenAI-compatible
        ``POST /v1/chat/completions`` non-stream path) that want one
        buffered completion body instead of a live frame stream.  R13
        dealign: this chunk-aggregation used to live in the route layer
        (``_openai_compat._stream_buffered_completions`` looped over
        frames itself); moving it onto the use case keeps the interface
        thin and the buffering an application concern.

        Drains the full :meth:`execute` iterator, concatenating every
        ``CHUNK`` frame's ``text`` payload (other frame types are
        observed but contribute no text), and returns the joined string.
        :class:`~qai.platform.errors.QaiError` raised while opening or
        draining the turn propagates to the caller unchanged.

        See :meth:`collect_completion` for the variant that also returns
        the cumulative token usage captured off the terminal END frame.
        """
        text, _usage = await self.collect_completion(request)
        return text

    async def collect_completion(
        self, request: StreamChatInput
    ) -> tuple[str, dict[str, Any] | None]:
        """Run a turn and return ``(text, usage)``.

        F-10 (GAP-REMEDIATION-PLAN §F-10): the OpenAI-compatible
        non-streaming path used to hard-code ``usage`` to character
        counts because :meth:`collect_completion_text` only returned
        the joined text and dropped the cumulative token usage carried
        on the terminal ``END`` frame (see :meth:`_drain_main_stream`
        L686 + :meth:`_handle_initial_tool_call` L830, which both
        capture ``payload["usage"]`` into ``state.turn_usage``).

        This method drains the full :meth:`execute` iterator, joining
        every ``CHUNK`` text and tracking the *last* ``END`` frame whose
        payload carries a ``usage`` dict (the snapshot end frame at
        :meth:`_finalize_assistant_message` L1396 carries no usage and
        is therefore correctly skipped). Returns the cumulative usage
        dict ({prompt_tokens, completion_tokens, total_tokens, ...}) or
        ``None`` if the upstream LLM did not report any.

        :class:`~qai.platform.errors.QaiError` raised while opening or
        draining the turn propagates to the caller unchanged.
        """
        parts: list[str] = []
        usage: dict[str, Any] | None = None
        agen = await self.execute(request)
        async for frame in agen:
            if frame.frame_type is StreamFrameType.CHUNK:
                t = frame.payload.get("text", "")
                if isinstance(t, str):
                    parts.append(t)
            elif frame.frame_type is StreamFrameType.END:
                # Capture token usage off the terminal frame.  Multiple
                # END frames may appear (real end + snapshot end); only
                # the real end carries ``usage``, so we keep the last
                # non-empty one we see (matching ``state.turn_usage``
                # accounting in :meth:`_drain_main_stream`).
                _u = frame.payload.get("usage")
                if isinstance(_u, dict):
                    usage = _u
        return "".join(parts), usage

    async def _abortable_frames(
        self,
        stream_frames: AsyncIterator[StreamFrame],
        handle: StreamAbortHandle,
    ) -> AsyncIterator[StreamFrame]:
        """Yield from ``stream_frames`` but stop promptly when ``handle`` fires.

        Thin shim delegating to
        :func:`qai.chat.application.use_cases._stream_guards.abortable_frames`
        (P4: the same helper the sub-agent loop now uses, single source
        of truth). Threads this use case's configured
        ``_frame_stall_budget_s`` so behaviour is byte-for-byte
        identical to the prior inline implementation.

        Zero-frame / blocked-first-token Stop failsafe (root-cause 2):
        the abort registry only *signals* an :class:`asyncio.Event`-backed
        handle; it does NOT cancel the in-flight upstream ``__anext__``.
        ``abortable_frames`` races each wait against a short poll window
        so a Stop that lands WHILE we are blocked on a silent/slow
        upstream releases the tab promptly (instead of hanging for the
        full 600s httpx read-timeout, the reported "发消息后未出输出就按
        停止 → 卡死").
        """
        async for frame in _shared_abortable_frames(
            stream_frames,
            handle,
            frame_stall_budget_s=self._frame_stall_budget_s,
        ):
            yield frame

    async def _abortable_sleep(
        self,
        seconds: float,
        handle: StreamAbortHandle | None,
    ) -> bool:
        """Sleep up to ``seconds``, returning early if ``handle`` fires.

        Used by the NETWORK auto-retry backoff so a user Stop interrupts a
        long (up to 30s) inter-attempt wait immediately instead of pinning the
        tab for the whole delay. Polls ``handle.is_set()`` in small slices
        (the handle exposes no awaitable Event — :class:`StreamAbortHandle` is
        ``is_set()``-only). Returns ``True`` if the abort fired during the
        wait (caller should stop retrying), ``False`` if the full delay
        elapsed. When ``handle`` is ``None`` it is a plain sleep.

        Also honours a "retry now" request (the "立即重试" button): when the
        handle's ``consume_retry_now`` fires, the wait ends early and returns
        ``False`` (NOT aborted) so the caller re-opens the stream at once —
        distinct from an abort (``True``, stops the turn). Handles predating
        retry-now are probed via ``getattr`` (legacy-safe).
        """
        if handle is None:
            await self._sleep(seconds)
            return False
        _consume_retry = getattr(handle, "consume_retry_now", None)
        _slice = 0.1
        waited = 0.0
        while waited < seconds:
            if handle.is_set():
                return True
            if _consume_retry is not None and _consume_retry():
                # User clicked "立即重试" — stop waiting, re-open now (continue).
                return False
            await self._sleep(min(_slice, seconds - waited))
            waited += _slice
        return handle.is_set()

    def _network_retry_frame(
        self,
        *,
        attempt: int,
        delay_seconds: float,
        code: Any = None,
    ) -> StreamFrame:
        """Build a non-terminal ``network_retry`` progress frame.

        Surfaced before each network-error backoff wait so the UI shows the
        "网络中断，正在等待恢复后自动重试 (N)…" banner (driving the existing
        ``networkRetry`` tab state) and the WS gets a positive keep-alive. The
        ``sequence`` is a local 0 — these frames are pure UI progress that are
        not part of the sequence-gap detection (mirrors the other transient
        progress frames; the frame_id keeps them unique).
        """
        return StreamFrame.network_retry(
            frame_id=f"net-retry-{self._ids.new_id()}",
            sequence=0,
            attempt=attempt,
            delay_seconds=delay_seconds,
            code=code if isinstance(code, str) else None,
        )

    async def _network_retrying_stream(
        self,
        open_stream: Callable[[], AsyncIterator[StreamFrame]],
        handle: StreamAbortHandle | None,
    ) -> AsyncIterator[StreamFrame]:
        """Wrap ``open_stream`` with the indefinite NETWORK auto-retry.

        Thin shim delegating to
        :func:`qai.chat.application.use_cases._stream_guards.network_retrying_stream`
        (P4: the same helper the sub-agent loop now uses, single source
        of truth). Threads this use case's ``_retry_policy`` /
        ``_classify_error_frame`` / ``_network_retry_frame`` / ``_sleep``
        so behaviour is byte-for-byte identical to the prior inline
        implementation — pure pass-through when ``_retry_policy`` is
        ``None`` (legacy / unit-stub parity), full first-frame
        inspection seam + abortable backoff otherwise.

        Shared by the follow-up (post-tool-call) rounds — the round-0
        path keeps its own inline copy inside :meth:`_open_with_retry`
        because it also threads PROMPT_TOO_LONG compression (a concern
        the sub-agent loop does not have).
        """
        async for frame in _shared_network_retrying_stream(
            open_stream,
            handle,
            retry_policy=self._retry_policy,
            classify_error_frame=self._classify_error_frame,
            build_retry_frame=lambda attempt, delay_seconds, code: (
                self._network_retry_frame(
                    attempt=attempt,
                    delay_seconds=delay_seconds,
                    code=code,
                )
            ),
            sleep=self._sleep,
            scope="followup_round",
        ):
            yield frame

    async def _drain_main_stream(
        self,
        *,
        stream_frames: AsyncIterator[StreamFrame],
        conv: Any,
        tab: Any,
        handle: StreamAbortHandle,
        request: "StreamChatInput",
        turn_started_ms: int,
        state: "_TurnBodyState",
    ) -> AsyncIterator[StreamFrame]:
        """Drain the initial LLM stream; yield frames; mutate ``state``.

        Main-loop slice of :meth:`_run` (B1 cohesion split).  Byte-for-byte
        identical to the inline ``async for frame in stream_frames`` body:

        * the abort handle short-circuits (sets ``state.aborted`` +
          ``state.abort_reason`` and stops);
        * CHUNK frames accumulate ``state.assistant_text_parts`` and stamp
          ``state.ttft_ms`` on the first visible chunk;
        * END frames capture ``state.turn_usage``;
        * SUBAGENT_* frames fold into ``state.sub_agent_blocks``;
        * a TOOL_CALL frame, when the agentic loop is enabled, hands off
          to :meth:`_handle_initial_tool_call` (drain remaining batched
          tool calls from the same turn + run the follow-up loop) and
          then stops the main loop; otherwise the legacy best-effort
          :meth:`_dispatch_tool` path runs and the frame is forwarded.

        Every published :class:`ChatStreamFrameEvent` + yield order +
        counter increment matches the original inline body.
        """
        # Race each frame-wait against the abort handle (zero-frame / blocked
        # first-token Stop, V1 chat_handler.py:702-706 interrupt parity). A
        # Stop that lands WHILE we are blocked in the upstream ``__anext__``
        # (silent/slow on-device daemon) would otherwise hang until the 600s
        # httpx timeout with the tab pinned STREAMING; ``_abortable_frames``
        # stops iteration promptly when ``handle`` fires so the abort tail can
        # release the tab. The per-frame ``handle.is_set()`` check below still
        # covers a Stop that lands between two delivered frames.
        # A self-contained agent (``query::*`` — MB Pro / CEBot) owns its OWN
        # round structure: the adapter already stamped each frame's
        # ``round_index`` (it is the round authority for that link, bypassing
        # this use case's agentic loop). For such turns we must PRESERVE the
        # adapter's ``round_index`` on forward rather than force round 0, or the
        # frontend collapses every frame into one round (all answer text piles
        # into a single bubble instead of interleaving with the per-round tool
        # cards). For ordinary cloud/local turns this is False and round 0 is
        # stamped exactly as before.
        _self_contained = _is_self_contained_agent(request.model_hint)
        async for frame in self._abortable_frames(stream_frames, handle):
            if handle.is_set():
                state.aborted = True
                state.abort_reason = handle.reason or "user_requested"
                break

            # Stamp ``emitted_at_ms`` on SUBAGENT_TOOL frames BEFORE publishing
            # so the live wire — not just the persisted fold — carries the
            # wall-clock the frontend's ``ToolExecPanel`` needs for
            # unmount-survival elapsed (sub-agent tool card parity with main
            # agent's TOOL_CALL path; see ``_stamp_emitted_at`` docstring).
            # ``_stamp_emitted_at`` is a no-op for other frame types so this
            # adds no behaviour for the rest of the main drain.
            if frame.frame_type is StreamFrameType.SUBAGENT_TOOL:
                frame = _stamp_emitted_at(frame, _now_ms(self._clock))

            # LIVE ↑badge口径 fix (must run BEFORE ``_publish`` below): a
            # single-round turn (hello/hi, no tool call) closes through THIS
            # main-drain END — NOT the follow-up loop — and its END frame is
            # PUBLISHED to the frontend right here. If we only re-stamped the
            # DISPLAY usage fields in the ``elif ... END`` branch further down
            # (which runs AFTER this publish), the frontend would receive an
            # END whose usage lacks ``last_round_cache_read_display`` /
            # ``last_round_cache_write_display`` → the badge falls back to
            # cacheRead=cacheWrite=0 and sums the WHOLE prompt as "new input"
            # (e.g. ↑7324 for a hello, ↑13000+ after a hi) until a reload
            # re-reads the persisted usage (→ ↑3, ↑6). Re-stamp the END payload
            # HERE so the PUBLISHED frame carries the same DISPLAY fields the
            # persisted message gets (:meth:`_finalize_assistant_message` via
            # the shared :meth:`_append_display_usage_fields`). Single-round ==
            # last_round == first_round, so no ``_finalize_turn_usage`` prompt
            # correction is needed; we only tail-append DISPLAY-ONLY keys.
            # CRITICAL (constraint: never break billing/counter): the
            # ``state.*_usage`` fields below are set from the ORIGINAL ``_u``
            # (NOT ``_end_usage``) — counter / ``assistant_eff_prompt`` read
            # those, and ``cache_read_tokens`` must stay whatever
            # ``_extract_usage`` set (zeroed on a cache-hit turn). The DISPLAY
            # fields live ONLY in the published/persisted frame payload. §3.1.
            if frame.frame_type is StreamFrameType.END:
                _pub_u = frame.payload.get("usage")
                if isinstance(_pub_u, dict):
                    _pub_end_usage = self._append_display_usage_fields(
                        _pub_u, _pub_u
                    )
                    _pub_payload = dict(frame.payload)
                    _pub_payload["usage"] = _pub_end_usage
                    frame = replace(frame, payload=_pub_payload)
            state.frame_count += 1
            await self._publish(
                ChatStreamFrameEvent(
                    tab_id=tab.id,
                    conversation_id=conv.id,
                    frame=frame,
                ),
            )

            if frame.frame_type is StreamFrameType.CHUNK:
                text = frame.payload.get("text", "")
                if isinstance(text, str):
                    if state.ttft_ms is None and text:
                        # V1 parity (useChat.js:2377): time-to-first-token
                        # = wall-clock from turn start to the first visible
                        # chunk. Persisted in meta.perf so a reload re-shows
                        # the latency metric.
                        state.ttft_ms = _now_ms(self._clock) - turn_started_ms
                    if _self_contained:
                        # Self-contained agent: capture text under its round so
                        # the reload rebuild (``build_tool_call_message``) can
                        # interleave it with the tool rounds in arrival order
                        # (mirrors the live per-round frontend rendering). Only
                        # the TRAILING text round (after the last tool) also
                        # flows through ``assistant_text_parts`` → the final
                        # assistant message, exactly like a normal turn's answer.
                        ri = _read_round_index(frame.payload)
                        if ri is not None:
                            state.chunk_text_by_round.setdefault(ri, []).append(
                                text
                            )
                        state.assistant_text_parts.append(text)
                    else:
                        state.assistant_text_parts.append(text)
            elif frame.frame_type is StreamFrameType.END:
                # Capture token usage off the terminal frame so the
                # finalised assistant Message can persist it (P1-4).
                #
                # NOTE: the END frame's ``usage`` was ALREADY re-stamped with
                # the DISPLAY-ONLY ``last_round_*`` / ``first_round_*`` keys in
                # the pre-publish block above (so the PUBLISHED live frame the
                # front-end badge reads carries them). Those keys are additive —
                # the original ``prompt_tokens`` / ``completion_tokens`` /
                # ``total_tokens`` / ``cache_read_tokens`` are unchanged — but
                # ``_accumulate_usage`` SUMs every integer key, so we MUST keep
                # the display keys OUT of the ``state.*_usage`` dicts that feed
                # the counter / persist口径. Strip them back to the original
                # ``_extract_usage`` shape here (billing/counter single-source
                # invariant); the DISPLAY keys survive ONLY in the published
                # frame payload + the persisted message (re-appended
                # idempotently by ``_finalize_assistant_message``). §3.1.
                _u_stamped = frame.payload.get("usage")
                if isinstance(_u_stamped, dict):
                    _u = {
                        k: v
                        for k, v in _u_stamped.items()
                        if not k.startswith("last_round_")
                        and not k.startswith("first_round_")
                    }
                    state.turn_usage = _u
                    # Single-round (non-agentic) turn: the only round IS the
                    # last round, so last_round_usage == turn_usage. The
                    # running full-history counter reads it as the wire size.
                    state.last_round_usage = _u
                    # Round 0 == the only round here, so it is also the FIRST
                    # round (per-message input tok/sec uses round-0 prompt).
                    state.first_round_usage = _u

            elif frame.frame_type is StreamFrameType.ERROR:
                # The LLM stream already surfaced a diagnostic error frame
                # (e.g. SSL failure, timeout, HTTP 4xx/5xx).  Mark the state
                # so ``_finalize_turn`` does NOT emit a redundant
                # ``empty_response`` that would overwrite the real error
                # message on the frontend (the real frame was already
                # published above via ``_publish``).
                state.had_llm_error = True
            elif frame.frame_type in _SUBAGENT_FRAME_TYPES:
                # V1 parity (useChat.js:62 / 2404): fold the relayed
                # sub-agent frame into a persistable block so a reload
                # re-renders the sub-agent fold blocks.
                _accumulate_sub_agent_block(state.sub_agent_blocks, frame)
            elif frame.frame_type is StreamFrameType.TOOL_CALL:
                if _self_contained:
                    # Self-contained agent (``query::*`` — MB Pro / CEBot): this
                    # TOOL_CALL is a DISPLAY card for a tool the agent already
                    # ran server-side (decision §2.8), NOT a request for the host
                    # to execute a tool and re-prompt. Do NOT enter the local
                    # follow-up loop and do NOT dispatch the tool locally — just
                    # collect it for persistence (preserving the adapter's
                    # ``round_index``) and let it fall through to the forward
                    # below. The agent streams its own ``tool_result`` cards + a
                    # terminal END, so the turn closes via the adapter's END (the
                    # main drain keeps iterating until that END exhausts the
                    # stream). Engaging the follow-up loop here re-ran the agent's
                    # tools locally + re-opened an LLM round that re-sent the
                    # prompt to the agent, so the turn never reached END and the
                    # UI stayed stuck "busy" after the agent had actually
                    # finished.
                    state.tc_frames.append(frame)
                elif self._is_followup_loop_enabled():
                    async for fr in self._handle_initial_tool_call(
                        initial_tool_call=frame,
                        stream_frames=stream_frames,
                        conv=conv,
                        tab=tab,
                        handle=handle,
                        request=request,
                        state=state,
                    ):
                        yield fr
                    # The follow-up loop swallows the trailing END frame;
                    # we MUST NOT yield the original frame twice and MUST
                    # NOT continue draining the main iterator (the
                    # original LLM stream is already exhausted by
                    # ``_run_followup_loop``).
                    break
                else:
                    # Legacy single-turn behaviour: dispatch the tool
                    # best-effort without consuming its result, then forward
                    # the original frame.
                    await self._dispatch_tool(
                        tab_id=tab.id,
                        conversation_id=conv.id,
                        frame=frame,
                    )
            elif (
                _self_contained
                and frame.frame_type is StreamFrameType.TOOL_RESULT
            ):
                # Self-contained agent's tool RESULT card. Ordinary cloud/local
                # turns never see a TOOL_RESULT in this initial drain (the host
                # produces results itself inside the follow-up loop), but a
                # self-contained agent streams call+result inline. Collect it
                # (preserving the adapter's ``round_index``) so the per-round
                # tool cards persist + re-render on reload, then forward below.
                state.tr_frames.append(frame)
            # Initial LLM call = agentic round 0: stamp its CHUNK /
            # TOOL_CALL frames so the frontend groups them under round 0
            # with zero inference (END / SUBAGENT_* etc. pass through).
            # Also stamp round 0's prompt-snapshot id (saved by
            # ``_open_initial_stream`` before the first frame) so live tool
            # cards / chunks bind to round 0's own snapshot.
            #
            # Self-contained agents are the round authority themselves (the
            # adapter already stamped ``round_index``): forward their frames
            # UNCHANGED so the per-round interleaving the adapter computed is
            # preserved (forcing round 0 here would collapse the whole turn into
            # one bubble). They also have no per-round host prompt snapshot, so
            # ``request_id`` stamping is skipped.
            if _self_contained:
                yield frame
            else:
                yield _stamp_request_id(
                    _stamp_round(frame, 0), state.round_request_ids.get(0),
                )

        # ── State-Truth-First reconciliation (zero-frame / blocked-wait Stop) ─
        # ``_abortable_frames`` stops iteration as soon as the handle fires —
        # even while blocked awaiting the FIRST token — by simply ``return``ing
        # WITHOUT yielding a frame. In that case the per-frame
        # ``if handle.is_set()`` check inside the loop above never runs, so
        # ``state.aborted`` would stay ``False`` and the turn would be wrongly
        # finalised as a normal (empty) completion — leaving the tab to
        # complete to IDLE silently instead of going through the ABORTED
        # interrupt path. Reconcile against the REAL abort signal once the
        # drain ends so an abort detected during a blocked wait is treated as
        # the interruption it is (same pattern as ``_handle_initial_tool_call``
        # for the intra-round race).
        if not state.aborted and handle.is_set():
            state.aborted = True
            state.abort_reason = handle.reason or "user_requested"

    async def _handle_initial_tool_call(
        self,
        *,
        initial_tool_call: StreamFrame,
        stream_frames: AsyncIterator[StreamFrame],
        conv: Any,
        tab: Any,
        handle: StreamAbortHandle,
        request: "StreamChatInput",
        state: "_TurnBodyState",
    ) -> AsyncIterator[StreamFrame]:
        """Drain the batched initial tool calls then run the follow-up loop.

        TOOL_CALL slice of :meth:`_run`'s main loop (B1 cohesion split),
        byte-for-byte identical to the inline ``if self._is_followup_loop_
        enabled():`` branch:

        1. forward + collect the initial TOOL_CALL frame for persistence;
        2. V1 chat_handler.py:576-587 parity — drain any remaining
           TOOL_CALL frames from the SAME LLM turn (cloud Claude's batched
           tool_use response) so they all reach the follow-up loop's first
           round and dispatch in parallel; CHUNK / non-terminal frames in
           the remainder are still forwarded in order, an END frame stops
           the drain;
        3. run :meth:`_run_followup_loop`, integrating each yielded frame
           into the turn (collect TOOL_CALL / TOOL_RESULT for persistence,
           capture follow-up CHUNK text + cumulative END usage, fold
           SUBAGENT_* blocks) and forwarding it.

        All counters / abort flags / accumulators are mutated on ``state``.
        """
        # Collect the initial TOOL_CALL frame for persistence (stamped as
        # agentic round 0 — it was issued by the initial LLM call).  Round 0's
        # snapshot was saved by ``_open_initial_stream`` before the first
        # frame, so stamp its ``request_id`` onto the round-0 frames here so
        # the initial tool cards bind to round 0's own prompt (V1 parity).
        _r0_rid = state.round_request_ids.get(0)
        initial_tc_r0 = _stamp_request_id(
            _stamp_round(initial_tool_call, 0), _r0_rid,
        )
        initial_tc_r0 = _stamp_emitted_at(
            initial_tc_r0, _now_ms(self._clock),
        )
        state.tc_frames.append(initial_tc_r0)
        yield initial_tc_r0
        state.synth_seq += 1
        extra_initial_tcs: list[StreamFrame] = []
        try:
            async for tail_frame in stream_frames:
                if handle.is_set():
                    state.aborted = True
                    state.abort_reason = handle.reason or "user_requested"
                    break
                if tail_frame.frame_type is StreamFrameType.TOOL_CALL:
                    # Batched initial tool calls from the SAME LLM turn ⇒
                    # still agentic round 0.
                    tail_tc_r0 = _stamp_request_id(
                        _stamp_round(tail_frame, 0), _r0_rid,
                    )
                    tail_tc_r0 = _stamp_emitted_at(
                        tail_tc_r0, _now_ms(self._clock),
                    )
                    state.tc_frames.append(tail_tc_r0)
                    extra_initial_tcs.append(tail_tc_r0)
                    await self._publish(
                        ChatStreamFrameEvent(
                            tab_id=tab.id,
                            conversation_id=conv.id,
                            frame=tail_tc_r0,
                        ),
                    )
                    yield tail_tc_r0
                    state.frame_count += 1
                    state.synth_seq += 1
                    continue
                if tail_frame.frame_type is StreamFrameType.END:
                    # End-of-turn marker — stop draining; the followup
                    # loop opens its own subsequent LLM rounds.
                    # Capture the initial turn's token usage so the
                    # follow-up loop can seed its running total (the
                    # terminal END then carries the summed turn usage —
                    # V1 useChat.js:2351-2356 accumulation parity).
                    _init_u = tail_frame.payload.get("usage")
                    if isinstance(_init_u, dict):
                        state.turn_usage = _init_u
                        # Round 0's own usage — its prompt_tokens is the
                        # round-0 wire prompt that matches ``ttft_ms`` (round-0
                        # prefill latency) for the per-message input tok/sec.
                        state.first_round_usage = _init_u
                        # Seed ``last_round_usage`` with round-0 too, so a turn
                        # that is INTERRUPTED during the follow-up loop (WS
                        # disconnect / upstream 400 / abort) before any
                        # follow-up round completes still updates the running
                        # full-history counter from a real measurement instead
                        # of leaving it ``None`` (which froze the badge — the
                        # very bug this fix targets). The follow-up loop's own
                        # ``_last_round_usage_holder`` OVERWRITES this with the
                        # latest completed round when one exists (see the
                        # ``if _last_round_usage_holder:`` branch after the
                        # loop), so the normal multi-round path is unchanged;
                        # this only fills the round-0-then-interrupt window.
                        state.last_round_usage = _init_u
                    break
                if tail_frame.frame_type is StreamFrameType.CHUNK:
                    text = tail_frame.payload.get("text", "")
                    if isinstance(text, str):
                        state.assistant_text_parts.append(text)
                # Forward CHUNK / non-terminal frames so the caller still
                # sees the full turn output (CHUNK stamped round 0; other
                # types pass through unchanged via ``_stamp_round``).
                fwd = _stamp_request_id(_stamp_round(tail_frame, 0), _r0_rid)
                await self._publish(
                    ChatStreamFrameEvent(
                        tab_id=tab.id,
                        conversation_id=conv.id,
                        frame=fwd,
                    ),
                )
                yield fwd
                state.frame_count += 1
                state.synth_seq += 1
        except StopAsyncIteration:  # pragma: no cover
            pass
        if state.aborted:
            return

        # Holder for the agentic turn's LAST-round usage (keystone of the
        # running full-history counter). ``_run_followup_loop`` is a generator
        # that does not receive ``state``; it writes the last round's usage
        # into this dict in place, which we copy onto ``state`` after the loop.
        _last_round_usage_holder: dict[str, Any] = {}
        async for fu_frame in self._run_followup_loop(
            tab=tab,
            conv=conv,
            initial_tool_call=initial_tool_call,
            extra_initial_tool_calls=extra_initial_tcs,
            handle=handle,
            assistant_text_parts=state.assistant_text_parts,
            request=request,
            seq_start=state.synth_seq,
            initial_usage=state.turn_usage,
            round_request_ids=state.round_request_ids,
            last_round_usage_out=_last_round_usage_holder,
            injected_messages_out=state.injected_messages,
        ):
            if fu_frame.frame_type is StreamFrameType.CHUNK:
                # Capture follow-up assistant text so it is persisted
                # with the rest of the turn.
                fu_text = fu_frame.payload.get("text", "")
                if isinstance(fu_text, str):
                    state.assistant_text_parts.append(fu_text)
            elif fu_frame.frame_type is StreamFrameType.TOOL_CALL:
                # Collect follow-up TOOL_CALL frames for persistence.
                fu_frame = _stamp_emitted_at(fu_frame, _now_ms(self._clock))
                state.tc_frames.append(fu_frame)
            elif fu_frame.frame_type is StreamFrameType.TOOL_RESULT:
                # Collect TOOL_RESULT frames for persistence — but ONLY the
                # final frame per tool call. A streaming (exec) tool emits
                # several ``partial=True`` increment frames before its single
                # ``partial=False`` final frame; collecting the partials too
                # would inflate ``tr_frames`` and shift positional pairing in
                # ``build_tool_call_message`` (the reported "exec output bound
                # to the write card / outputs empty after reload" bug). The
                # partials are still forwarded/yielded below for live UI.
                if fu_frame.payload.get("partial") is not True:
                    state.tr_frames.append(fu_frame)
                    # Incremental durability (V1 frontend-save parity, done
                    # server-side): a tool round just produced its final
                    # result. Persist everything completed so far NOW, so a
                    # backend kill / restart / crash mid-turn -- which runs no
                    # ``except`` block -- still leaves the completed rounds on
                    # disk. Idempotent: rebuilds the turn tail from the
                    # accumulated frames each time (no duplicate stacking).
                    # Best-effort: a persistence hiccup must never break the
                    # live stream.
                    try:
                        await self._persist_completed_rounds(
                            conv=conv,
                            now=self._clock.now(),
                            state=state,
                        )
                    except Exception:  # noqa: BLE001 - never break the stream
                        _log.warning(
                            "chat.incremental_round_persist_failed",
                            conversation_id=conv.id.value,
                        )
            elif fu_frame.frame_type is StreamFrameType.ERROR:
                # A follow-up (post-tool-call) round surfaced a diagnostic
                # ERROR frame (e.g. transient ``chat.llm.connect_error`` /
                # timeout on the next LLM request). Mark the turn so
                # ``_finalize_turn`` does NOT overwrite the real error with a
                # redundant ``empty_response``; the frame itself is forwarded +
                # published below (then the emitter's terminal END(failed)
                # closes the turn). Parity with the round-0 ERROR branch in
                # ``_drain_main_stream`` (streaming.py ~1763).
                state.had_llm_error = True
            elif fu_frame.frame_type is StreamFrameType.END:
                # Last END frame of the agentic turn carries the
                # cumulative usage; keep the latest seen.
                _fu_u = fu_frame.payload.get("usage")
                if isinstance(_fu_u, dict):
                    state.turn_usage = _fu_u
            elif fu_frame.frame_type in _SUBAGENT_FRAME_TYPES:
                # V1 parity (useChat.js:62 / 2404): fold the relayed
                # sub-agent frame into a persistable block so a reload
                # re-renders the fold blocks.
                # SUBAGENT_TOOL is stamped with ``emitted_at_ms`` BEFORE
                # both the fold and the live publish below so the
                # frontend's sub-agent tool card gets the wall-clock for
                # unmount-survival elapsed (sibling fold persists ``ts``;
                # see ``_stamp_emitted_at`` docstring + main-drain stamp
                # site above for the parity rationale).
                if fu_frame.frame_type is StreamFrameType.SUBAGENT_TOOL:
                    fu_frame = _stamp_emitted_at(
                        fu_frame, _now_ms(self._clock),
                    )
                _accumulate_sub_agent_block(state.sub_agent_blocks, fu_frame)
            await self._publish(
                ChatStreamFrameEvent(
                    tab_id=tab.id,
                    conversation_id=conv.id,
                    frame=fu_frame,
                ),
            )
            yield fu_frame
            state.frame_count += 1
            state.synth_seq += 1
            if handle.is_set():
                state.aborted = True
                state.abort_reason = handle.reason or "user_requested"
                break

        # ── State-Truth-First reconciliation (root cause: intra-round race) ─
        # When the abort is detected mid-stream INSIDE a follow-up round,
        # ``_drain_followup_round`` short-circuits with ``outcome.aborted=True``
        # and ``_run_followup_loop`` returns WITHOUT yielding another frame.
        # The per-frame ``handle.is_set()`` check above (line ~875) then never
        # runs (no trailing frame), so ``state.aborted`` would stay ``False``
        # and the turn would be (wrongly) finalised as a normal completion —
        # stamping ``meta.perf`` instead of ``meta.interrupted`` and dropping
        # the interrupt marker. Reconcile ``state.aborted`` against the REAL
        # abort signal (the handle) once the loop drains, so an intra-round
        # abort is treated as the interruption it is.
        if not state.aborted and handle.is_set():
            state.aborted = True
            state.abort_reason = handle.reason or "user_requested"

        # Surface the agentic turn's last-round usage onto state so
        # ``_finalize_assistant_message`` can tail-append the keystone
        # ``last_round_prompt_tokens`` and update the running counter. Falls
        # back to ``turn_usage`` (already set from the terminal END) when the
        # loop produced no per-round usage.
        if _last_round_usage_holder:
            state.last_round_usage = dict(_last_round_usage_holder)
        elif state.last_round_usage is None:
            state.last_round_usage = state.turn_usage

    @staticmethod
    def _is_subagent_takeover_turn(request: "StreamChatInput") -> bool:
        """True when this turn is a RESOLVED user TAKE-OVER of a sub-agent.

        A take-over is dispatched on the PARENT conversation's chat WS with
        ``extra["subagent_id"]`` set (see ``interfaces/http/routes/chat/_ws.py``).
        Its authoritative transcript lives ONLY in ``SubAgentSession.messages``
        (persisted by :meth:`_persist_subagent_takeover`), NOT in the parent
        conversation.

        Discriminator: the ``_subagent_takeover`` marker set by
        :meth:`_maybe_load_subagent_takeover` ONLY when the ``subagent_id``
        actually resolves to a live session. A ``subagent_id`` that does NOT
        resolve degrades to a NORMAL parent-conversation turn (see
        ``test_unknown_subagent_id_falls_back_to_normal_turn``) and MUST still
        persist to the parent — so we key on the resolved marker, NOT the raw id.

        LEAK FIX (2026-07-11): the parent-conversation persistence sites
        (:meth:`_finalize_turn` and the interrupt / error tails) previously wrote
        the take-over user message + tool-call round (including the sub-agent's
        ``question`` tool_call) + assistant text into the parent ``chat_message``
        rows with the parent ``conversation_id`` and NO discriminator. On a
        service restart the parent transcript was re-hydrated from those rows, so
        the sub-agent's last message leaked into the MAIN agent's history.
        """
        extra = getattr(request, "extra", None)
        if not isinstance(extra, dict):
            return False
        return extra.get("_subagent_takeover") is True

    async def _save_parent_conv(
        self, conv: Any, *, is_takeover: bool
    ) -> None:
        """Persist ``conv`` to the parent conversation store — UNLESS this is a
        sub-agent take-over turn (``is_takeover``).

        Single choke point for every parent-conversation ``save_messages``
        during a turn. In-memory ``conv.messages`` mutations are left intact
        (wire building / event publishing are unchanged); only the durable write
        to the parent ``chat_message`` rows is suppressed for take-over turns,
        because that turn is persisted separately into ``SubAgentSession``. See
        :meth:`_is_subagent_takeover_turn`.
        """
        if is_takeover:
            return
        await self._conversations.save_messages(conv)

    async def _prepare_turn(
        self,
        request: StreamChatInput,
    ) -> tuple[Any, Any, Message, StreamAbortHandle]:
        """Load + lock the turn: conv/tab, user message, abort handle.

        Setup slice of :meth:`_run` (B1 cohesion split).  Byte-for-byte
        identical to the inline preamble:

        1. load the conversation aggregate + tab; raise
           :class:`ConversationLockedError` if the tab is bound to a
           different conversation;
        2. transition the tab IDLE -> STREAMING + persist;
        3. append the user message (stamping ``meta.image_url`` from the
           first media ref for V1 image-preview reload parity) + publish
           :class:`MessageAppendedEvent`;
        4. register the abort handle BEFORE the LLM opens so a racing
           :class:`StopChatUseCase` always finds something to signal;
        5. publish :class:`ChatStreamStartedEvent`.

        Returns ``(conv, tab, user_msg, handle)``.
        """
        conv = await self._conversations.get(request.conversation_id)
        tab = await self._tabs.get(request.tab_id)

        # CCD-5: lazy-load this conversation's persisted compaction checkpoint
        # into the in-memory write-through cache (at most once per conversation
        # per process). Done in this async preamble so the downstream SYNC wire
        # builders / badge estimator hit the in-memory dict (they cannot await).
        # No-op when no durable store is wired (pure-memory mode unchanged).
        await self._lazy_load_checkpoint(conv)

        if tab.conversation_id != request.conversation_id:
            raise ConversationLockedError(
                request.conversation_id.value,
                tab.id.value,
                message=(
                    f"tab {tab.id.value!r} is bound to a different "
                    "conversation"
                ),
            )

        # ── Recover an ABORTED tab from a PRIOR turn (V1 sendable parity) ──
        # A user Stop transitions the tab STREAMING → ABORTED (a meaningful
        # domain terminal). But the next message reuses the SAME tab id
        # (``OpenTabUseCase`` is idempotent and returns the existing record
        # unchanged), so without this the new turn's ``start_stream()`` would
        # raise ``requires status=IDLE, got aborted`` and the conversation
        # would go dead after a single Stop ("发消息后按停止 → 后续消息无反应").
        # V1 has no separate ABORTED terminal: its ``finally`` resets
        # ``isStreaming=false`` so the input box is always re-enabled after a
        # Stop (useChat.js:2747-2750). We honour that "Stop → re-sendable"
        # semantics by recovering an ABORTED tab back to IDLE at the START of
        # the next turn (the ABORTED state already did its job — persisting the
        # interrupt marker on the prior turn). CLOSED tabs are NOT auto-revived
        # (a closed tab is intentionally gone).
        if tab.status is TabStatus.ABORTED:
            tab.status = TabStatus.IDLE

        # ── Recover a STREAMING tab whose prior turn ended without cleanup ──
        # State-Truth-First (AGENTS.md 🔴 铁律5): a tab can be stuck in
        # STREAMING in the DB if ``_release_streaming_tab``'s save() failed
        # (e.g. SQLite lock) during the prior turn's exception handler. The
        # abort registry is the in-memory ground truth for "is a stream ACTUALLY
        # running right now": if the registry has NO active handle for this tab,
        # the prior stream has already ended (the ``finally`` block unregistered
        # it) and the STREAMING state is stale. Recover it to IDLE so the user
        # is not permanently locked out ("tab 卡在 streaming, 后续消息无反应").
        # If the registry DOES have an active handle, a concurrent stream is
        # genuinely in progress and ``start_stream()`` should rightly reject.
        if tab.status is TabStatus.STREAMING:
            if not self._abort_registry.is_streaming(tab.id):
                _log.warning(
                    "chat.tab_stale_streaming_recovered",
                    tab_id=tab.id.value,
                    conversation_id=tab.conversation_id.value,
                )
                tab.status = TabStatus.IDLE

        # Transition tab IDLE -> STREAMING (raises TabStateError if already
        # streaming on this tab).
        tab.start_stream(now=self._clock.now())
        await self._tabs.save(tab)

        # Append user message.
        # V1 parity (useChat.js:57-58): persist the attached image URL under
        # the message meta so a history reload re-renders the image preview.
        # The web UI carries the persisted ``/api/images/...`` URL in the
        # user content's ``media_refs``; stamp the first one as ``image_url``.
        _user_image_url = (
            request.user_message.media_refs[0]
            if request.user_message.media_refs
            else None
        )
        user_msg = self._build_message(
            role=MessageRole.USER,
            content=request.user_message,
            now=self._clock.now(),
            parent_id=conv.last_message().id if conv.last_message() else None,
            meta=({"image_url": _user_image_url} if _user_image_url else None),
        )
        conv.append_message(user_msg)
        # LEAK FIX (2026-07-11): the user-message persistence is deferred to
        # ``_run`` (right after ``_maybe_load_subagent_takeover`` resolves
        # whether this is a REAL take-over), because the ``_subagent_takeover``
        # discriminator is not set until then. ``_prepare_turn`` only appends the
        # message in memory + publishes the live event here; a non-take-over turn
        # is persisted by ``_run`` immediately, a resolved take-over turn is not
        # (its transcript goes to ``SubAgentSession`` instead). This keeps a
        # ``subagent_id`` that does NOT resolve (fallback → normal turn) correctly
        # persisting to the parent.
        await self._publish(
            MessageAppendedEvent(
                conversation_id=conv.id,
                message_id=user_msg.id,
                role=user_msg.role.value,
                appended_at=user_msg.created_at,
            ),
        )

        # Register the abort handle BEFORE opening the LLM stream so a
        # racing StopChatUseCase always finds something to signal.
        handle = self._abort_handle_factory()
        # Registry may raise ConversationLockedError; let it surface.
        self._abort_registry.register(tab_id=tab.id, handle=handle)

        await self._publish(
            ChatStreamStartedEvent(
                tab_id=tab.id,
                conversation_id=conv.id,
                started_at=self._clock.now(),
            ),
        )
        return conv, tab, user_msg, handle

    def _detect_effective_mode(
        self,
        *,
        request: StreamChatInput,
        requested_tool_mode: str | None,
    ) -> str | None:
        """Resolve the effective tool mode via the system-prompt builder.

        Thin wrapper over :func:`_streaming_helpers.detect_effective_mode`
        (ARCH-1 / A-3 cohesion split): forwards ``self._system_prompt_builder``
        explicitly; logic byte-for-byte unchanged.
        """
        return _detect_effective_mode_fn(
            system_prompt_builder=self._system_prompt_builder,
            request=request,
            requested_tool_mode=requested_tool_mode,
        )

    async def _run(
        self,
        request: StreamChatInput,
    ) -> AsyncIterator[StreamFrame]:
        conv, tab, user_msg, handle = await self._prepare_turn(request)

        # ── Pre-stream setup ─────────────────────────────────────────────────
        # State-Truth-First (AGENTS.md 🔴 铁律5): every line below runs AFTER
        # ``_prepare_turn`` has transitioned the tab to STREAMING and persisted
        # it. If ANY of these raise before reaching the main ``try`` block, the
        # tab would be permanently pinned STREAMING (no release path covers
        # them). Wrap the setup in its own guard so a failure here still
        # releases the tab back to IDLE — the exact same defensive pattern as
        # the main-loop ``except Exception`` arm.
        try:
            # Runtime debug flags (forge-config service_launch) — read ONCE per
            # turn so an operator edit is picked up live. Stash on ``request.extra``
            # (the original dict the snapshot helpers read) under private keys that
            # ``_CHAT_CONTROL_KEYS`` keeps off the wire. ``prompt_debug`` drives the
            # pre-send console dump; ``show_prompt_in_ui`` gates the snapshot save
            # (V1 ``main.py:1360`` parity).
            _prompt_debug, _show_prompt_in_ui = await self._load_runtime_debug_flags()
            if _prompt_debug or not _show_prompt_in_ui:
                if request.extra is None:
                    object.__setattr__(request, "extra", {})
                if isinstance(request.extra, dict):
                    request.extra["_prompt_debug"] = _prompt_debug
                    request.extra["_show_prompt_in_ui"] = _show_prompt_in_ui

            # User take-over of a sub-agent (``extra["subagent_id"]``): load the
            # sub-agent's persisted wire history into ``extra`` so the base-wire
            # builder uses it (instead of the parent conversation) and the tool
            # collector restricts to the sub-agent set. Best-effort + no-op when
            # the repo is unwired / the id does not resolve. Returns the loaded
            # working session so the turn can persist the user's round back to it.
            takeover_session = await self._maybe_load_subagent_takeover(request)

            # LEAK FIX (2026-07-11): now that ``_maybe_load_subagent_takeover``
            # has resolved whether this is a REAL take-over (it sets the
            # ``_subagent_takeover`` marker only when the ``subagent_id`` resolves
            # to a live session), persist the user message appended in
            # ``_prepare_turn``. For a normal turn (incl. a ``subagent_id`` that
            # did NOT resolve → fallback) this writes to the parent conversation
            # exactly as before; for a resolved take-over it is a no-op (the turn
            # is persisted into ``SubAgentSession`` by
            # ``_persist_subagent_takeover``), so the sub-agent's messages never
            # leak into the parent ``chat_message`` rows / MAIN agent history.
            await self._save_parent_conv(
                conv, is_takeover=self._is_subagent_takeover_turn(request)
            )

            # P11 — single source of truth for take-over wire construction.
            # The previous design built the take-over wire in TWO places:
            # :meth:`_build_base_wire_messages` (followup rounds +
            # ``compact_hook`` re-base) AND :meth:`_build_llm_request`
            # (round 0). The two implementations diverged in their user-turn
            # construction (the round-0 one preserved multimodal vision
            # blocks via the pre-assembled ``extra["messages"]`` last
            # element; the followup one always used plain text — report
            # A.2 divergence 1) and required a ``_is_followup_round`` guard
            # to prevent round-0 logic re-running on follow-up rounds and
            # producing a wire ending with the round's trailing assistant
            # message (HTTP 400 prefill, the original fix #3).
            #
            # Plan X collapses the two paths: ``_build_base_wire_messages``
            # is now the single constructor (multimodal-aware via
            # :meth:`_resolved_user_turn`). We invoke it HERE — exactly
            # once, in the SAME pre-stream window where
            # ``_maybe_load_subagent_takeover`` stashed
            # ``_subagent_takeover_wire`` — and stash the result onto
            # ``request.extra["messages"]`` so the adapter picks it up on
            # round 0 EXACTLY like it picked up the previous in-place
            # ``_build_llm_request`` take-over slot. On follow-up rounds
            # ``_run_followup_loop`` overwrites ``extra["messages"]`` with
            # its kernel-grown send wire (the existing contract); on
            # ``compact_hook`` the same ``_build_base_wire_messages``
            # function rebuilds from the same source, so the multimodal
            # user turn is preserved end-to-end (closes P12).
            #
            # The ``_subagent_takeover_wire`` / ``_subagent_takeover``
            # markers are intentionally left on ``extra`` here — the
            # advertise-side ``_collect_tool_schemas`` still reads
            # ``_subagent_takeover`` for the sub-agent tool set, and
            # ``_build_llm_request`` pops both before they reach the wire
            # payload (so they never leak to the upstream).
            if isinstance(request.extra, dict) and isinstance(
                request.extra.get("_subagent_takeover_wire"), list
            ):
                _takeover_wire_initial = self._build_base_wire_messages(
                    conv=conv,
                    request=request,
                    compressed_history=None,
                )
                request.extra["messages"] = _takeover_wire_initial

            # Operator hook: session start (once per turn, mirrors the legacy
            # harness ON_SESSION_START at run_turn entry).
            await self._fire_hook(
                HookEvent.ON_SESSION_START,
                payload={"conversation_id": conv.id.value, "tab_id": tab.id.value},
            )
            # Operator hook: user input (once per turn, right after session
            # start — the user's message has been received and is about to be
            # processed by the agent loop). Payload carries the user's text so
            # an operator hook can log "what the user asked" to an audit
            # pipeline or fold turn-specific context in via ``additional_context``
            # (spliced into this turn's system prompt, same channel as
            # PRE_MESSAGE). An interceptor ``additional_context`` here takes
            # effect for the whole turn.
            _user_input_rec = await self._fire_hook(
                HookEvent.ON_USER_INPUT,
                payload={
                    "conversation_id": conv.id.value,
                    "tab_id": tab.id.value,
                    "text": getattr(request.user_message, "text", "") or "",
                },
            )
            if (
                _user_input_rec is not None
                and _user_input_rec.additional_context
                and isinstance(request.extra, dict)
            ):
                request.extra["hook_context"] = _user_input_rec.additional_context
        except Exception:
            # Pre-stream setup failed — release the STREAMING tab so it does
            # not stay permanently dead. Best-effort (shield + swallow).
            try:
                await asyncio.shield(
                    self._release_streaming_tab(tab=tab, now=self._clock.now())
                )
            except Exception:  # noqa: BLE001
                _log.warning(
                    "chat.tab_release_on_pre_stream_failed",
                    tab_id=tab.id.value,
                )
            raise

        # V1 parity (useChat.js:2377): server-side turn timing so a reloaded
        # turn can re-show a perf line (ttft_ms / total_ms). The fine-grained
        # tok/sec rates remain client-computed off the live stream; this is
        # the persisted baseline so the metric is not lost on reload.
        _turn_started_ms = _now_ms(self._clock)
        # All the mutable turn accumulators (assistant text, tool-call /
        # tool-result frames for persistence, usage, sub-agent fold blocks,
        # TTFT, frame_count, synth_seq, abort flags) live in this holder so
        # the extracted ``_drain_main_stream`` body (B1 cohesion split) can
        # mutate them in place.  ``synth_seq`` starts at 1_000_000 — a
        # non-clashing counter for use-case-synthesised frames (adapter
        # frames carry their own sequence numbers).
        state = _TurnBodyState(
            assistant_text_parts=[],
            tc_frames=[],
            tr_frames=[],
            sub_agent_blocks={},
            # LEAK FIX (2026-07-11): flag this turn as a sub-agent take-over so
            # every parent-conversation persistence site (finalize / interrupt /
            # error tails, several of which have no ``request`` in scope) can
            # skip the parent DB write via ``_save_parent_conv``. The take-over
            # turn is persisted into ``SubAgentSession`` instead.
            is_subagent_takeover=self._is_subagent_takeover_turn(request),
        )
        # Record the persistence baseline: ``_prepare_turn`` has already
        # appended + saved the user message, so everything this turn adds from
        # here on lives at ``conv.messages[baseline:]``. Incremental per-round
        # persistence rebuilds that tail idempotently (see
        # ``_persist_completed_rounds``), so a process kill mid-turn still
        # leaves every completed tool round on disk (V1 frontend-save parity,
        # done server-side).
        state.turn_persist_baseline = len(conv.messages)

        # ── tool_mode_changed detection ──────────────────────────────
        # Discover the effective tool mode (which may differ from the
        # explicit request.tool_mode due to auto-detection); a change
        # yields a tool_mode_changed frame BEFORE the first CHUNK
        # (legacy ``backend/chat_handler.py:463-467``).
        requested_tool_mode: str | None = (
            (request.extra or {}).get("tool_mode") if request.extra else None
        )
        # Tool-mode AUTO-DETECTION only runs when the user is in NO mode at all.
        # Two cases skip it (keeping the explicitly-requested mode as-is):
        #
        #  (1) The user is ALREADY in a toolbar mode (``requested_tool_mode`` is
        #      set — model-build / app-builder / code / translate / ppt / pro).
        #      Auto-detect must NOT override a mode the user explicitly chose:
        #      e.g. while in 编程 mode, asking a question containing "模型…量化"
        #      must NOT yank the toolbar into 模型构建器. The user's active mode
        #      is authoritative; only a no-mode turn may be auto-classified.
        #      (This is enforced here in the use case — not left to the
        #      builder's internal ``if not tool_mode`` guard — so the rule is
        #      explicit and robust to future builder changes.)
        #
        #  (2) Self-contained query-service agents (``query::*`` — CEBot,
        #      MB Pro) bring their own prompt/routing and do NOT consume the V2
        #      SystemPromptBuilder output, so keyword auto-detection is
        #      meaningless and merely mis-fires (e.g. a CEBot question
        #      "大语言模型如何量化？" matches 模型…量化 → spurious
        #      tool_mode_changed → UI yanked into 模型构建器). Consistent with
        #      the ``_self_contained`` gate at the agentic-loop level.
        #
        # Ordinary cloud/local chat with NO active mode is unaffected —
        # auto-detect still runs and can flip the toolbar exactly as before.
        if requested_tool_mode or _is_self_contained_agent(request.model_hint):
            effective_mode = requested_tool_mode
        else:
            effective_mode = self._detect_effective_mode(
                request=request, requested_tool_mode=requested_tool_mode,
            )

        try:
            # Emit tool_mode_changed if the effective mode differs from
            # the explicitly requested mode.
            if effective_mode and effective_mode != requested_tool_mode:
                state.synth_seq += 1
                mode_frame = StreamFrame.tool_mode_changed(
                    frame_id=f"tmc-{state.synth_seq}",
                    sequence=state.synth_seq,
                    mode=effective_mode,
                    previous_mode=requested_tool_mode,
                )
                await self._publish(
                    ChatStreamFrameEvent(
                        tab_id=tab.id,
                        conversation_id=conv.id,
                        frame=mode_frame,
                    ),
                )
                yield mode_frame
                state.frame_count += 1

            # ----- P0-2: pre-send context compression (V1 chat_handler.py:419-450) -----
            # Before opening the LLM stream, check whether the conversation
            # history occupies > 80% of the model's context window. If so,
            # compress it proactively to avoid cloud provider rejections
            # (especially for models with small context like qwen 8K or
            # doubao 128K that V2 might misestimate as 200K).
            async for _compact_frame in self._maybe_presend_compress(
                conv=conv, request=request, state=state
            ):
                yield _compact_frame

            # Operator hook: pre-message (user message about to enter the LLM).
            # An interceptor hook may return ``additional_context`` to fold into
            # this turn's system prompt (e.g. current git branch, a lint
            # summary). Stashed under ``extra["hook_context"]`` so the system
            # prompt builder splices it in (mirrors the ``memory_context``
            # mechanism). No directive → nothing injected (unchanged).
            _pre_msg_rec = await self._fire_hook(HookEvent.PRE_MESSAGE)
            if (
                _pre_msg_rec is not None
                and _pre_msg_rec.additional_context
                and isinstance(request.extra, dict)
            ):
                request.extra["hook_context"] = _pre_msg_rec.additional_context

            # ── Zero-frame abort guard (V1 chat_handler.py:702-706 "Phase 1B
            # 中断检查" parity) ──────────────────────────────────────────────
            # Very common user action: send a question, then immediately hit
            # Stop BEFORE the model has produced any output. The abort handle
            # is signalled while we are still about to open (or are blocked
            # opening) the LLM stream; the main-loop ``handle.is_set()`` check
            # only fires once the FIRST frame arrives, so a silent/slow
            # on-device daemon would leave us blocked in ``__anext__`` with the
            # tab pinned STREAMING until the 600s httpx timeout — a dead tab.
            # V1 checks its interrupt flag at the TOP of each round, before
            # opening that round's stream; we mirror that here so an
            # already-signalled abort short-circuits the open entirely.
            if handle.is_set():
                state.aborted = True
                state.abort_reason = handle.reason or "user_requested"
            else:
                stream_frames = self._open_initial_stream(
                    conv=conv, tab=tab, request=request,
                    round_request_ids=state.round_request_ids,
                    handle=handle,
                )
                # ``stream_frames`` is an AsyncIterator already drained one
                # frame ahead by ``_open_initial_stream`` when retry policy is
                # wired; otherwise it is the raw LLM iterator.  The whole
                # body-drain (CHUNK accumulation / END usage / sub-agent fold /
                # TOOL_CALL inline execution + follow-up loop) lives in
                # :meth:`_drain_main_stream`, which mutates ``state`` in place.
                # ``_drain_main_stream`` itself now also races the FIRST-frame
                # wait against the abort handle so a Stop that lands WHILE we
                # are blocked awaiting the first token is honoured promptly
                # (not only after a frame finally arrives).
                async for frame in self._drain_main_stream(
                    stream_frames=stream_frames,
                    conv=conv,
                    tab=tab,
                    handle=handle,
                    request=request,
                    turn_started_ms=_turn_started_ms,
                    state=state,
                ):
                    yield frame
        except asyncio.CancelledError:
            # V1 PARITY (useChat.js:2747-2771): a page refresh / SSE client
            # disconnect cancels the producer task running ``_run`` (see
            # interfaces/http/routes/chat/_sse.py:_with_heartbeat finally →
            # producer_task.cancel()). The cancellation surfaces here as
            # ``CancelledError`` (NOT ChatStreamAbortedError, NOT a plain
            # Exception). Without flushing, every completed tool round was
            # lost on refresh, leaving only the user message ("刷新后只剩 1
            # ``轮"). Persist the accumulated rounds, then re-raise so the
            # cancellation still propagates (asyncio contract). ``shield``
            # protects the flush from the in-flight cancellation.
            _cancel_now = self._clock.now()

            async def _flush_and_mark_interrupted() -> None:
                _last = await self._flush_tool_calls_on_interrupt(
                    conv=conv, user_msg=user_msg,
                    now=_cancel_now, state=state,
                )
                # V1 PARITY (useChat.js:2688-2712): a refresh/disconnect is
                # an interruption too — persist the meta.interrupted marker
                # so the reload shows the turn was cut short.
                await self._persist_interrupted_assistant_message(
                    conv=conv, user_msg=user_msg, now=_cancel_now,
                    state=state, last_tool_msg=_last,
                )
                # State-Truth-First (AGENTS.md 🔴 铁律5 异常路径必须兜底 + 铁律1
                # 缓存脱节必须纠偏): a refresh/disconnect persisted the partial
                # turn into ``conv`` (DB grew) but historically never advanced
                # ``full_history_tokens`` — so the reloaded badge froze, 脱节
                # from the wire the model actually received this turn. Advance
                # the counter with this turn's measured last-round wire (same口径
                # as the normal finalize) so a reopen reflects真实占用.
                await self._update_full_history_counter_on_interrupt(
                    conv=conv, state=state,
                    model_hint=request.model_hint or "",
                )
                # State-Truth-First (AGENTS.md 🔴 铁律3): durably persist any
                # mid-turn user injection BEFORE the ``finally`` clears the
                # registry — otherwise an injection made in the round that got
                # cancelled (refresh / disconnect) is lost from the DB even
                # though the model already saw it on the wire.
                await self._persist_injections_on_interrupt(
                    conv=conv, state=state,
                )
                # State-Truth-First (AGENTS.md 🔴 铁律5 异常路径必须兜底 + 🟡🟡):
                # a standalone sub-agent take-over turn's history lives ONLY in
                # ``SubAgentSession.messages`` (no parent ``conv.messages`` copy
                # backs it up). The normal-completion path persists it at the
                # very end of ``_run`` (after ``_finalize_turn``), which a
                # refresh/disconnect CancelledError NEVER reaches — so the whole
                # take-over turn was lost from the sub-agent's transcript and the
                # next round's model read no longer saw it ("失忆"). Persist it
                # here too, in parity with the tool-flush / injection / tab-release
                # interrupt fallbacks above. Best-effort + idempotent at the call
                # site (the CancelledError re-raises below, so the normal-path
                # persist is never reached → no double-append). ``takeover_session``
                # is ``None`` for a non-take-over turn → a cheap no-op.
                await self._persist_subagent_takeover(
                    takeover_session, request=request, state=state,
                )
                # State-Truth-First (AGENTS.md 🔴 铁律5 + V1 useChat.js:2748
                # ``finally`` unconditional ``isStreaming=false``): a
                # refresh/disconnect must NOT leave the tab pinned in
                # STREAMING, or the next ``start_stream()`` raises
                # ``requires status=IDLE`` and the tab is dead. Release it
                # back to a fresh, sendable IDLE.
                await self._release_streaming_tab(tab=tab, now=_cancel_now)

            try:
                await asyncio.shield(_flush_and_mark_interrupted())
            except Exception:  # noqa: BLE001 — never mask the cancellation
                _log.warning(
                    "chat.tool_flush_on_cancel_failed",
                    conversation_id=conv.id.value,
                )
            raise
        except ChatStreamAbortedError:
            state.aborted = True
            await self._fire_hook(
                HookEvent.ON_ERROR, payload={"category": "aborted"}
            )
        except GeneratorExit:
            # State-Truth-First (AGENTS.md 🔴 铁律5 + V1 useChat.js:2748
            # unconditional ``isStreaming=false``): the CONSUMER abandoned the
            # stream early and ``aclose()``d us — e.g. the frontend bailed out
            # the instant it received an ERROR frame (local service down →
            # ``chat.local.service_unavailable``), so it never drained the
            # adapter's trailing END nor let us reach the post-loop
            # ``_finalize_turn`` → ``_complete_turn`` that resets the tab.
            # Without releasing here the tab stayed pinned STREAMING and every
            # later ``start_stream()`` was rejected with ``requires status=
            # IDLE`` — the reported "服务未启动报错后, 此后所有新提问都没有反应"
            # dead-tab bug (same class as the Stop-button deadlock).
            #
            # ``GeneratorExit`` is a ``BaseException`` (NOT caught by the
            # generic ``except Exception`` below), raised at the suspended
            # ``yield`` when the consumer calls ``aclose()``. Awaiting here is
            # legal as long as we do NOT ``yield`` again; we release the tab
            # then re-raise so the generator closes cleanly (asyncio contract).
            try:
                _gx_now = self._clock.now()
                # Flush parity with the CancelledError / Exception paths: a
                # consumer ``aclose()`` mid-turn must also persist completed
                # tool rounds. Incremental per-round saves already cover most
                # of this; this catches a round that finished between the last
                # incremental save and the close. Idempotent (truncate +
                # rebuild) so it never stacks duplicates.
                try:
                    await self._flush_tool_calls_on_interrupt(
                        conv=conv, user_msg=user_msg, now=_gx_now, state=state,
                    )
                    # State-Truth-First (AGENTS.md 🔴 铁律3): durably persist
                    # any mid-turn user injection BEFORE the ``finally`` clears
                    # the registry. Awaiting a save here is already done by the
                    # flush above (this path is under ``BaseException`` but does
                    # not ``yield`` again), so the injection save is legal too.
                    await self._persist_injections_on_interrupt(
                        conv=conv, state=state,
                    )
                    # State-Truth-First (AGENTS.md 🔴 铁律5 + 🟡🟡): persist the
                    # standalone sub-agent take-over turn on a consumer
                    # ``aclose()`` too — its history has NO parent ``conv``
                    # backup, so without this the turn vanishes from the
                    # sub-agent transcript (the "失忆" bug). Parity with the
                    # CancelledError / Exception fallbacks; no ``yield`` here so
                    # the await is legal under GeneratorExit. The GeneratorExit
                    # re-raises below → the normal-path persist is never reached
                    # → no double-append. No-op for a non-take-over turn.
                    await self._persist_subagent_takeover(
                        takeover_session, request=request, state=state,
                    )
                except Exception:  # noqa: BLE001 — never mask the GeneratorExit
                    _log.warning(
                        "chat.tool_flush_on_close_failed",
                        tab_id=tab.id.value,
                        conversation_id=conv.id.value,
                    )
                await self._release_streaming_tab(
                    tab=tab, now=_gx_now,
                )
            except Exception:  # noqa: BLE001 — never mask the GeneratorExit
                _log.warning(
                    "chat.tab_release_on_close_failed",
                    tab_id=tab.id.value,
                    conversation_id=conv.id.value,
                )
            raise
        except Exception as exc:  # noqa: BLE001
            # V1 PARITY (frontend useChat.js:2747-2771 ``finally`` save):
            # any mid-loop failure (e.g. a compression-induced
            # ``AttributeError``, a provider error, or an unexpected raise
            # from the follow-up loop) MUST still flush whatever tool
            # rounds already completed, otherwise the whole turn's tool
            # history is lost and the user sees only their own message
            # after a refresh. V1's frontend kept every completed round in
            # ``messages.value`` and saved it in ``finally``; V2 persists
            # server-side, so we replicate that durability guarantee here
            # by persisting the accumulated tool frames before re-raising.
            state.run_error = exc
            try:
                await self._flush_tool_calls_on_interrupt(
                    conv=conv, user_msg=user_msg, now=self._clock.now(),
                    state=state,
                )
            except Exception:  # noqa: BLE001 — never mask the original error
                _log.warning(
                    "chat.tool_flush_on_error_failed",
                    tab_id=tab.id.value,
                    conversation_id=conv.id.value,
                )
            # State-Truth-First (AGENTS.md 🔴 铁律5 异常路径必须兜底 + 🟡🟡): the
            # standalone sub-agent take-over turn's history has NO parent ``conv``
            # backup, so a mid-loop exception (which re-raises below and never
            # reaches the normal-path persist) would lose the whole take-over turn
            # from the sub-agent transcript ("失忆"). Persist it here too — parity
            # with the tool-flush fallback above and the CancelledError /
            # GeneratorExit handlers. Shielded + best-effort so a save failure
            # never masks the original error; the re-raise below guarantees the
            # normal-path persist is not also reached → no double-append. No-op
            # for a non-take-over turn (``takeover_session`` is ``None``).
            try:
                await asyncio.shield(
                    self._persist_subagent_takeover(
                        takeover_session, request=request, state=state,
                    )
                )
            except Exception:  # noqa: BLE001 — never mask the original error
                _log.warning(
                    "chat.subagent_takeover_on_error_failed",
                    tab_id=tab.id.value,
                    conversation_id=conv.id.value,
                )
            # State-Truth-First (AGENTS.md 🔴 铁律5 异常路径必须兜底 + 铁律1
            # 缓存脱节必须纠偏): a mid-loop exception persisted the partial turn
            # into ``conv`` (DB grew) but historically never advanced
            # ``full_history_tokens``. Advance the running counter with this
            # turn's measured last-round wire (same口径 as the normal finalize)
            # so the reloaded badge reflects真实占用 instead of the stale value.
            await self._update_full_history_counter_on_interrupt(
                conv=conv, state=state,
                model_hint=request.model_hint or "",
            )
            # State-Truth-First (AGENTS.md 🔴 铁律5 异常路径必须兜底 + V1
            # useChat.js:2748 ``finally`` unconditional ``isStreaming=false``):
            # an unexpected mid-loop exception (provider crash, malformed
            # on-device frame, a bug in the follow-up loop) must NEVER leave
            # the tab pinned in STREAMING. The previous code only flushed tool
            # rounds and re-raised; the ``finally`` below only unregisters the
            # abort handle, so the tab stayed STREAMING forever and every
            # subsequent ``start_stream()`` raised ``requires status=IDLE,
            # got streaming`` — the user's whole tab went dead until reload
            # (the reported "tab 卡在 streaming, 后续消息无反应" bug). Release
            # it back to a sendable IDLE so a single failed turn cannot brick
            # the tab. Shielded + best-effort: a save failure here must not
            # mask the original error.
            try:
                await asyncio.shield(
                    self._release_streaming_tab(
                        tab=tab, now=self._clock.now(),
                    )
                )
            except Exception:  # noqa: BLE001 — never mask the original error
                _log.warning(
                    "chat.tab_release_on_error_failed",
                    tab_id=tab.id.value,
                    conversation_id=conv.id.value,
                )
            await self._fire_hook(
                HookEvent.ON_ERROR, payload={"category": "exception"}
            )
            raise
        finally:
            # NOTE: keep this block side-effect-only + non-awaiting beyond
            # the existing sync unregister.  ``_run`` is an async generator;
            # if the consumer ``aclose()``s it early (SSE client disconnect)
            # the ``finally`` runs under ``GeneratorExit`` where awaiting is
            # illegal.  The ON_SESSION_END / ON_COMPLETE hooks are therefore
            # fired at the explicit normal/abort exits below, not here.
            #
            # NOTE: the normal-completion path already releases the handle
            # eagerly in ``_complete_turn`` (so the registry is empty BEFORE
            # ``end``/``done`` reach the consumer — queue re-send race fix).
            # This ``unregister`` therefore primarily backstops the EARLY-EXIT
            # paths (abort / exception / GeneratorExit / client disconnect)
            # that never reach ``_complete_turn``. It is idempotent
            # (``pop(..., None)``), so a double-unregister after a normal
            # completion is a harmless no-op.
            self._abort_registry.unregister(tab.id)
            # Mid-turn user injection (V2 enhancement): clear the tab's
            # PENDING injection registry (process-local transient holding texts
            # not yet drained into a round). The already-drained injections were
            # minted into ``state.injected_messages`` and have, by this point,
            # been durably persisted to ``conv`` on EVERY termination path —
            # the normal path (``_finalize_turn`` / ``_persist_completed_rounds``
            # → ``_reinsert_injected_messages``) and the interrupt/abort paths
            # (``_persist_injections_on_interrupt`` runs in the CancelledError /
            # GeneratorExit / abort handlers BEFORE this clear). Clearing the
            # registry here is therefore safe: it only drops PENDING texts that
            # were never drained (no minted message, never sent to the model),
            # which are intentionally not carried over to a fresh turn.
            # Synchronous + side-effect-only, so it is safe under
            # ``GeneratorExit``.
            if self._injection_registry is not None:
                self._injection_registry.clear(tab.id)

        now = self._clock.now()
        if state.aborted:
            # V1 PARITY (useChat.js:2747-2771): an aborted turn (user hit
            # Stop, or the SSE client disconnected on a page refresh) must
            # STILL persist the tool rounds that already completed — V1's
            # frontend kept them in ``messages.value`` and saved in
            # ``finally``. Without this, refreshing mid-task wiped the whole
            # tool-call history, leaving only the user message ("刷新后只剩
            # 1 轮"). Flush the accumulated tool frames before marking the
            # tab aborted.
            _last_tool_msg = await self._flush_tool_calls_on_interrupt(
                conv=conv, user_msg=user_msg, now=now, state=state,
            )
            # V1 PARITY (useChat.js:2688-2712): persist the user-visible
            # interrupt marker (meta.interrupted=True) so a reload still
            # shows the turn was interrupted — not just the tool cards.
            await self._persist_interrupted_assistant_message(
                conv=conv, user_msg=user_msg, now=now, state=state,
                last_tool_msg=_last_tool_msg,
            )
            # State-Truth-First (AGENTS.md 🔴 铁律5 异常路径必须兜底 + 铁律1
            # 缓存脱节必须纠偏): a user Stop / abort persisted the partial turn
            # into ``conv`` (DB grew) but historically never advanced
            # ``full_history_tokens`` — the badge would freeze at the pre-abort
            # value on reload. Advance the counter with this turn's measured
            # last-round wire (same口径 as the normal finalize) so真实占用 shows.
            await self._update_full_history_counter_on_interrupt(
                conv=conv, state=state,
                model_hint=request.model_hint or "",
            )
            # State-Truth-First (AGENTS.md 🔴 铁律3): durably persist any
            # mid-turn user injection on the abort (user Stop) path too, BEFORE
            # the ``finally`` clears the registry — an injection made in the
            # aborted round must not be lost from the DB.
            await self._persist_injections_on_interrupt(
                conv=conv, state=state,
            )
            # State-Truth-First (AGENTS.md 🔴 铁律5 异常路径必须兜底 + 🟡🟡): an
            # ABORTED take-over turn (user hit Stop, ``ChatStreamAbortedError``
            # caught above WITHOUT re-raise, or the zero-frame abort guard) falls
            # through to this block and ``return``s — it NEVER reaches the
            # normal-path ``_persist_subagent_takeover`` at the end of ``_run``.
            # The standalone sub-agent tab's history has no parent ``conv`` backup,
            # so without persisting here the aborted take-over turn vanishes from
            # the transcript ("失忆"). Persist it now, in parity with the tool-flush
            # / interrupt-marker / injection saves above. The ``return`` below
            # guarantees the normal-path persist is not also reached → no
            # double-append. No-op for a non-take-over turn.
            await self._persist_subagent_takeover(
                takeover_session, request=request, state=state,
            )
            # Operator hooks: session end on the abort exit.
            await self._fire_hook(HookEvent.ON_SESSION_END)
            await self._handle_abort(
                tab=tab,
                conv=conv,
                now=now,
                abort_reason=state.abort_reason,
                frame_count=state.frame_count,
            )
            return

        # Normal completion: persist tool-call / tool-result messages +
        # the final assistant message, emit the turn-warning frame, and
        # complete the tab.  The whole tail lives in :meth:`_finalize_turn`
        # which threads counters through ``state``.
        async for fr in self._finalize_turn(
            conv=conv,
            tab=tab,
            user_msg=user_msg,
            request=request,
            now=now,
            turn_started_ms=_turn_started_ms,
            state=state,
        ):
            yield fr

        # Operator hook: post message (the assistant's reply for this turn is
        # fully finalized + persisted). Fires BEFORE on_complete / on_session_end
        # (mirrors the lifecycle order pre_message → … → post_message →
        # on_complete → on_session_end). Payload carries the final assistant
        # text and whether any tool calls ran this turn, so an operator hook can
        # e.g. run tests / format / push a notification after each reply. Purely
        # observational (any interceptor directive is ignored — the turn is done).
        await self._fire_hook(
            HookEvent.POST_MESSAGE,
            payload={
                "conversation_id": conv.id.value,
                "tab_id": tab.id.value,
                "text": "".join(state.assistant_text_parts),
                "used_tools": bool(state.tc_frames),
            },
        )

        # Operator hooks: normal-completion exit — on_complete then
        # session_end (mirrors the legacy harness ON_COMPLETE +
        # ON_SESSION_END ordering at run_turn's success path).
        await self._fire_hook(HookEvent.ON_COMPLETE)
        await self._fire_hook(HookEvent.ON_SESSION_END)

        # Persist the user's take-over turn back onto the sub-agent session
        # (shared-ownership: the main agent can later wake it to read this).
        # Best-effort, after the normal-completion tail.
        await self._persist_subagent_takeover(
            takeover_session, request=request, state=state,
        )

    async def _maybe_load_subagent_takeover(
        self,
        request: StreamChatInput,
    ) -> Any | None:
        """Load the sub-agent session for a user take-over turn.

        When ``extra["subagent_id"]`` is present and the repo is wired and the
        id resolves, stash the sub-agent's prior wire history under
        ``extra["_subagent_takeover_wire"]`` (so :meth:`_build_base_wire_messages`
        uses it as the base context instead of the parent conversation) and set
        ``extra["_subagent_takeover"] = True`` (so :meth:`_collect_tool_schemas`
        restricts to the sub-agent tool set — no nested ``agent``).

        Returns the loaded :class:`SubAgentSession` (working aggregate) so the
        caller can persist the user's turn back, or ``None`` when this is not a
        take-over (no repo / no id / id unresolved → normal conversation turn).
        Best-effort: any failure degrades to a normal turn.
        """
        repo = self._sub_agent_sessions
        extra = request.extra
        if repo is None or not isinstance(extra, dict):
            return None
        raw_id = extra.get("subagent_id")
        if not isinstance(raw_id, str) or not raw_id:
            return None
        try:
            sid = SubAgentSessionId(raw_id)
        except (ValueError, TypeError):
            return None
        try:
            session = await repo.find(sid)
        except Exception as exc:  # noqa: BLE001 — never block the turn
            _log.warning(
                "chat.subagent_takeover.load_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None
        if session is None:
            return None
        # SUBAGENT-UNIFY-6: seed the base wire from the AUTHORITATIVE structured
        # transcript (``session.messages``) via the SAME
        # ``rebuild_history_wire_messages`` the main agent uses — so a take-over
        # feeds the model from the structured truth (thought_signature on the
        # structured cards is carried back onto the rebuilt wire). The legacy
        # ``wire_messages`` column is gone; ``messages`` is the SOLE source. Drop
        # the leading system turn (the adapter prepends the system prompt). Keep
        # a copy so concurrent mutation of the aggregate does not affect it.
        rebuilt = _rebuild_history_wire_messages(tuple(session.messages))
        prior_wire = [
            dict(m) for m in rebuilt if m.get("role") != "system"
        ]
        # Repair any orphan ``role=tool`` rows (a ``role=tool`` whose nearest
        # non-tool predecessor is NOT an ``assistant{tool_calls}``) BEFORE the
        # wire is fed to the LLM. A sub-agent history that was itself produced
        # by a prior take-over (whose appended assistant turn carries no
        # ``tool_calls``) or was interrupted mid-round can strand a tool row;
        # the infrastructure pre-send sanitiser would otherwise DELETE it,
        # breaking the OpenAI assistant↔tool sequence → upstream 200 empty
        # stream → ``empty_response`` (the reported take-over failure). The
        # repair folds the orphan's output into adjacent assistant text
        # (information-preserving) instead of dropping it, so the request stays
        # structurally valid. Clean histories pass through unchanged.
        prior_wire = _repair_orphan_tool_messages(prior_wire)
        extra["_subagent_takeover_wire"] = prior_wire
        extra["_subagent_takeover"] = True
        return session

    async def _persist_subagent_takeover(
        self,
        session: Any | None,
        *,
        request: StreamChatInput,
        state: "_TurnBodyState",
    ) -> None:
        """Append the user's take-over turn + assistant reply to the session.

        Best-effort: rebuilds the sub-agent wire as ``prior + user + assistant``
        and records it so a later main-agent wake (or a re-open) sees the
        user-driven conclusion. No-op when not a take-over.
        """
        if session is None or self._sub_agent_sessions is None:
            return
        extra = request.extra if isinstance(request.extra, dict) else {}
        # Gate: only persist when this turn was actually a take-over (the
        # marker is set by _maybe_load_subagent_takeover).
        if not isinstance(extra.get("_subagent_takeover_wire"), list):
            return

        # State-Truth-First (AGENTS.md 🔴 铁律1 真实状态优先 + 🟡🟡 发现缺陷必须
        # 修): the standalone sub-agent tab's history lives ONLY in
        # ``session.messages`` (no parent ``conv.messages`` copy backs it up), so
        # losing this save = the user-visible turn truly vanishes ("失忆"). The
        # save is a compare-and-swap on ``version``; a slow take-over turn
        # (many tools / long latency) can be版本-bumped under it by a concurrent
        # writer — most commonly the PATCH sub-agent model route
        # (``interfaces/http/routes/chat/_rest.py`` → ``subagent_sessions.save``,
        # which bumps version without adding a round). That bumps the stored
        # version so this turn's CAS save matches 0 rows → ``SubAgentSession
        # ConflictError``. The previous implementation let the broad ``except
        # Exception`` SWALLOW that conflict (only a ``persist_failed`` warning) —
        # the whole take-over turn was lost. Now we RETRY (reload-replay-restore):
        # on each attempt we RE-LOAD the latest session (latest messages + latest
        # version) and rebuild the take-over working copy on top of THAT base, so
        # a concurrent writer's content is PRESERVED (never clobbered) and the
        # CAS re-aligns to the fresh version. Bounded retries; on exhaustion we
        # still degrade best-effort (never block the turn) but log loudly.
        _sid = session.id
        _base: Any | None = session
        _max_attempts = 3
        for _attempt in range(1, _max_attempts + 1):
            if _base is None:
                # Reload failed to resolve the session — cannot rebuild a
                # faithful base; stop (logged below as exhaustion / failure).
                break
            try:
                working = self._build_takeover_working(
                    _base, request=request, state=state,
                )
                await self._sub_agent_sessions.save(working)
                return
            except SubAgentSessionConflictError as conflict:
                # CAS lost: a concurrent writer bumped the version mid-turn.
                # Reload the LATEST session as the new base and replay this
                # turn on top of it (preserving the concurrent content), then
                # retry the save with the fresh version. ⚠️ We MUST rebuild on
                # the freshly-loaded messages — reusing the stale ``_base``
                # would overwrite (lose) the concurrent writer's turns.
                if _attempt >= _max_attempts:
                    _log.warning(
                        "chat.subagent_takeover.persist_conflict_exhausted",
                        subagent_id=_sid.value,
                        attempts=_attempt,
                        expected_version=getattr(
                            conflict, "expected_version", None
                        ),
                    )
                    return
                try:
                    _base = await self._sub_agent_sessions.find(_sid)
                except Exception as reload_exc:  # noqa: BLE001
                    _log.warning(
                        "chat.subagent_takeover.persist_reload_failed",
                        subagent_id=_sid.value,
                        attempt=_attempt,
                        error=str(reload_exc),
                        error_type=type(reload_exc).__name__,
                    )
                    return
            except Exception as exc:  # noqa: BLE001 — never block the turn
                # Any non-conflict failure stays best-effort (the turn must
                # never be blocked by a persistence error), but is logged.
                _log.warning(
                    "chat.subagent_takeover.persist_failed",
                    subagent_id=_sid.value,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                return

    def _build_takeover_working(
        self,
        base_session: Any,
        *,
        request: StreamChatInput,
        state: "_TurnBodyState",
    ) -> Any:
        """Build the USER_OWNED working copy for a take-over save.

        Pure builder: takes a *base* sub-agent session (the LATEST persisted
        truth — on a retry this is the freshly-reloaded session, so a concurrent
        writer's turns are carried forward) and returns a fresh working aggregate
        whose transcript is ``base.messages`` + THIS take-over turn (user + tool
        rounds + assistant). ``version`` / ``rounds`` are derived from
        ``base_session`` so the save's compare-and-swap re-aligns to the latest
        stored version each attempt. Usage is folded ONCE per built working copy
        (``accumulate_usage`` adds onto the base's cumulative figure), so a retry
        — which discards the previous working and builds anew on a fresh base —
        adds this turn's usage exactly once; it is never double-counted.
        """
        extra = request.extra if isinstance(request.extra, dict) else {}
        user_text = getattr(request.user_message, "text", "") or ""
        assistant_text = "".join(state.assistant_text_parts).strip()
        # SUBAGENT-UNIFY-6: the canvas is the AUTHORITATIVE structured transcript
        # (``base_session.messages``) — the legacy ``wire_messages`` column is
        # gone. We rebuild the canvas wire from the structured messages via the
        # SAME ``rebuild_history_wire_messages`` the resume / take-over LOAD path
        # uses (口径 parity with the main agent's cross-turn rebuild), then append
        # THIS take-over turn (user + tool rounds + assistant) and derive the new
        # structured transcript from the result. The rebuild RETAINS the system
        # turn (role=SYSTEM Message → ``{"role": "system", ...}``) so a later
        # main-agent wake / re-open still has the sub-agent's system prompt (fix:
        # 🟡-1 — without this the session permanently lost its system turn after
        # the first take-over). ``rebuild_history_wire_messages`` never emits an
        # orphan ``role:tool`` row (it drops any call whose ``output`` was lost
        # and pairs 1:1), so the structured canvas is structurally clean by
        # construction; the ``_repair_orphan_tool_messages`` calls below stay as
        # a cheap, idempotent belt-and-suspenders.
        new_wire: list[dict[str, Any]] = _rebuild_history_wire_messages(
            tuple(base_session.messages),
        )
        # Self-heal: repair any orphan ``role=tool`` rows before persisting so a
        # later take-over / wake never re-loads a structurally broken history
        # (the orphan would otherwise be deleted by the pre-send sanitiser and
        # break the request — the ``empty_response`` root cause). Information-
        # preserving (folds the orphan output into adjacent assistant text);
        # clean histories are unchanged.
        new_wire = _repair_orphan_tool_messages(new_wire)
        if user_text:
            new_wire.append({"role": "user", "content": user_text})
        # Append THIS take-over turn's tool rounds as full OpenAI wire — the
        # SAME completeness口径 as a sub-agent's autonomous run. We reconstruct
        # the rounds from this turn's ``tc_frames`` / ``tr_frames`` via the SAME
        # kernel wire builders + round grouping the parent conversation
        # persistence (``build_tool_call_message``) uses, so the rebuilt wire is
        # the authoritative, complete transcript that ``_wire_to_structured_
        # messages`` then folds into ``session.messages`` (the SOLE truth source
        # the standalone sub-agent tab + a later main-agent wake both read). The
        # earlier implementation appended ONLY ``user`` + final ``assistant``
        # text and DROPPED every ``assistant{tool_calls}`` + paired ``role:tool``
        # block — so a take-over turn that called tools left NOTHING of those
        # tools in the transcript (the reported "工具卡 + 总结跑完就消失 / 刷新后
        # 没有" bug).
        new_wire.extend(_takeover_turn_wire_blocks(state))
        if assistant_text:
            new_wire.append({"role": "assistant", "content": assistant_text})
        # Belt-and-suspenders (Fix C): repair AGAIN after the append so the
        # PERSISTED wire is guaranteed orphan-free regardless of what the prior
        # wire ended with. ``repair`` is idempotent + cheap on a clean wire, so
        # this never harms a well-formed history but prevents writing back a
        # structurally-broken wire that a later wake/reopen would loop on.
        new_wire = _repair_orphan_tool_messages(new_wire)
        # TAKEOVER-USAGE-STAMP: stamp this take-over turn's per-message usage
        # onto the wire BEFORE converting to structured messages, so the
        # sub-agent tab's ``cumulativeInputNew`` (which reads ``message.usage``)
        # can show ↑↓ for user-owned sessions — matching the autonomous path
        # (agent_tool.py:1904-1915). ``state.last_round_usage`` is the same
        # usage dict already fed to ``_record_subagent_turn_usage`` below (the
        # scalar-badge path), so the口径 is identical. Only stamp when we have
        # real usage; no-op for local / no-measure turns. Idempotent: skips a
        # turn that already carries usage so a retry never double-stamps.
        if isinstance(state.last_round_usage, dict) and state.last_round_usage:
            # A take-over appends a single final assistant turn — walk backwards
            # to the last assistant turn and stamp it (unlike the autonomous
            # path, which spans multiple rounds).
            for _i in range(len(new_wire) - 1, -1, -1):
                _t = new_wire[_i]
                if isinstance(_t, dict) and _t.get("role") == "assistant":
                    if not _t.get("usage"):  # idempotent
                        _t["usage"] = _append_display_usage_fields_shared(
                            dict(state.last_round_usage),
                            state.last_round_usage,
                        )
                    break
        _log.debug(
            "chat.diag.takeover_usage_stamp",
            subagent_id=getattr(base_session, "id", None),
            stamped=(
                isinstance(state.last_round_usage, dict)
                and bool(state.last_round_usage)
            ),
            last_round_prompt_tokens=(
                (state.last_round_usage or {}).get("prompt_tokens")
            ),
            cache_read_observed=(
                (state.last_round_usage or {}).get("cache_read_observed")
            ),
        )
        # The loaded session may be terminal (DONE/ERROR/INTERRUPTED) — a
        # prior completed sub-agent run. Rebuild a fresh USER_OWNED working
        # copy (same id, carrying its created_at) so record_round is
        # permitted (terminal sessions reject it), mirroring the resume
        # path's fresh-copy strategy.
        from qai.chat.domain.sub_agent_session import (
            SubAgentOwner as _Owner,
            SubAgentSession as _Session,
            SubAgentSessionStatus as _Status,
        )

        now = self._clock.now()
        # State-Truth-First (铁律 1) + 拍板 rule 4: ``base_session.model_id`` is
        # the authoritative budget-denominator真值源, BUT a take-over turn
        # carries the user's latest model choice in ``request.model_hint``
        # (the standalone tab's own selected model). When the user switched
        # the sub-agent's model in the tab and then took it over, that newest
        # selection is the authority — persist it onto the session so a later
        # GET / wake reads the model the user actually ran the take-over with.
        # Provider is carried (when the client supplied it) in
        # ``extra["tool_params"]["selected_model_provider"]`` (same口径 as the
        # main turn's provider extraction); absent → keep the prior provider
        # if the model is unchanged, else None (id-only lookup still resolves).
        _prior_model_id = getattr(base_session, "model_id", None)
        _prior_model_provider = getattr(base_session, "model_provider", None)
        _req_model_id = request.model_hint or None
        _tp = extra.get("tool_params") if isinstance(extra, dict) else None
        _req_provider = (
            _tp.get("selected_model_provider")
            if isinstance(_tp, dict)
            else None
        )
        if _req_model_id is not None and _req_model_id != _prior_model_id:
            # User ran the take-over with a different model than the session
            # last recorded → that newest choice wins (authority).
            _eff_model_id = _req_model_id
            _eff_model_provider = _req_provider
        else:
            # Same model (or no hint this turn) → preserve the persisted
            # truth, only adopting a freshly-supplied provider for the same id.
            _eff_model_id = _prior_model_id
            _eff_model_provider = (
                _req_provider
                if _req_provider is not None
                else _prior_model_provider
            )
        working = _Session(
            id=base_session.id,
            root_conversation_id=base_session.root_conversation_id,
            parent_message_id=base_session.parent_message_id,
            # Tree edges are an identity property of the node — a user
            # take-over does not restructure the tree (alpha unified spawn
            # path, migration 049).
            parent_subagent_id=base_session.parent_subagent_id,
            depth=base_session.depth,
            subagent_type=base_session.subagent_type,
            title=base_session.title,
            prompt_preview=base_session.prompt_preview,
            status=_Status.USER_OWNED,
            owner=_Owner.USER,
            rounds=base_session.rounds + 1,
            created_at=base_session.created_at,
            updated_at=now,
            # Carry the LATEST loaded version so the take-over save does a
            # correct compare-and-swap (block 4). On a retry ``base_session`` is
            # the freshly-reloaded session, so this is the up-to-date version the
            # concurrent writer left behind → the CAS re-aligns instead of losing
            # again. getattr keeps older aggregates (no version attr) working.
            version=getattr(base_session, "version", 0),
            # Carry the sub-agent's OWN model (migration 046) — the
            # budget-denominator真值源. ``request.model_hint`` (the user's
            # latest tab selection) takes priority over the prior persisted
            # model per the authority rule above; provider follows the same
            # decision. ``None`` on a legacy session → budget readers fall
            # back as before (no regression).
            model_id=_eff_model_id,
            model_provider=_eff_model_provider,
            # SUBAGENT-UNIFY-6: the take-over turn's AUTHORITATIVE structured
            # transcript (the SOLE truth source), derived from ``new_wire``
            # via the SHARED converter — so a take-over session's standalone
            # tab reads these structured messages identically to an
            # autonomously-run one, and a later main-agent wake rebuilds the
            # feed-the-model wire from them. thought_signature on the rebuilt
            # wire's tool_calls is carried through the wire entries
            # themselves (``new_wire`` already carries them).
            messages=_wire_to_structured_messages(
                new_wire, ids=self._ids, now=now,
            ),
        )
        # State-Truth-First (铁律 1): update the standalone tab's context-badge
        # numerator (``last_prompt_tokens``) from THIS take-over turn's real
        # provider usage. A user-driven (take-over) turn runs through the main
        # ``StreamChatUseCase`` chain, which only advances ``conv.full_history_
        # tokens`` — it NEVER touched ``SubAgentSession.last_prompt_tokens``, so
        # a sub-agent that was ONLY ever conversed-with via take-over kept
        # ``last_prompt_tokens=None`` forever and its tab badge read "~0.0K"
        # (the reported bug — DB showed every ``user_owned`` session with
        # ``last_prompt_tokens=None`` while every autonomously-run ``done``
        # session had a real value). Feed the turn's last-round usage through
        # the SAME ``accumulate_usage`` the autonomous ``agent_tool`` path uses
        # so the口径 is identical: prefer the provider-measured prompt, adding
        # the Anthropic cache split (cache_read, falling back to cache_write
        # when the gateway reports the volume there) back into the effective
        # wire size. No-op when this turn carried no usage (local / no-measure)
        # — the prior value is preserved (never regresses to 0). Recorded via
        # the SHARED ``record_subagent_turn_usage`` helper — single source of
        # truth with the autonomous ``agent_tool._on_round_end`` path ②, so
        # the cache口径 (cache_read + cache_write fallback for Anthropic) and
        # the ``last_round_prompt_tokens`` replace-last injection stay
        # byte-for-byte identical between the two sub-agent links.
        #
        # Usage is folded ONCE per built working copy. ``accumulate_usage`` adds
        # onto ``base_session``'s cumulative figure; a retry rebuilds on a fresh
        # base (whose cumulative已含 every committed turn but NOT this one) and
        # discards the prior failed working, so this turn's usage is added
        # exactly once — never double-counted across retries.
        _record_subagent_turn_usage(
            working,
            state.last_round_usage,
            model_id=_eff_model_id,
            now=now,
        )
        return working

    async def _finalize_turn(
        self,
        *,
        conv: Any,
        tab: Any,
        user_msg: Message,
        request: "StreamChatInput",
        now: datetime,
        turn_started_ms: int,
        state: "_TurnBodyState",
    ) -> AsyncIterator[StreamFrame]:
        """Normal-completion tail of :meth:`_run` (B1 cohesion split).

        Byte-for-byte identical to the inline tail:

        1. persist any agentic-loop tool calls as one ASSISTANT message
           (:meth:`_build_tool_call_message`);
        2. if assistant text was produced, finalise the assistant message
           + yield the snapshot END frame (:meth:`_finalize_assistant_message`);
           else if only tool messages exist, just save the conversation;
           else emit the empty-response ERROR frame;
        3. emit the V1 ``turn_warning`` frame when a new threshold crosses
           (:meth:`_emit_turn_warning`);
        4. complete the tab + publish completion (:meth:`_complete_turn`).

        Counters / the finalised assistant message thread through ``state``.
        """
        # Idempotent with incremental per-round persistence
        # (:meth:`_persist_completed_rounds`): that path may already have
        # written this turn's tool messages to ``conv`` on each round. Drop
        # them and rebuild the consolidated per-round messages once here so the
        # normal-completion path never double-appends what the incremental
        # saves left behind.
        _baseline = state.turn_persist_baseline
        if _baseline >= 0 and len(conv.messages) > _baseline:
            del conv.messages[_baseline:]
        # Self-contained agent (``query::*`` — MB Pro): the answer text arrived
        # as per-round CHUNK frames interleaved with the tool rounds. Hand the
        # per-round text to the builder so each round message carries ITS round's
        # text (matching the live order on reload), and keep only the TRAILING
        # text (after the last tool round) for the final assistant message. For
        # ordinary turns ``chunk_text_by_round`` is empty → ``text_by_round`` is
        # ``None`` and ``joined`` is the whole answer, exactly as before.
        _text_by_round: dict[int, str] | None = None
        if state.chunk_text_by_round:
            _text_by_round = {
                ri: "".join(parts)
                for ri, parts in state.chunk_text_by_round.items()
            }
        tool_call_msg = self._build_tool_call_message(
            now=now,
            user_msg=user_msg,
            conv=conv,
            tc_frames=state.tc_frames,
            tr_frames=state.tr_frames,
            text_by_round=_text_by_round,
        )

        # Determine parent for the final assistant text message.
        _final_parent_id: MessageId | None = (
            tool_call_msg.id if tool_call_msg is not None else user_msg.id
        )

        assistant_msg: Message | None = None
        if _text_by_round is not None and state.tc_frames:
            # Self-contained agent WITH tool rounds: the inter-tool text is now
            # carried by the per-round messages built above; the final assistant
            # message holds ONLY the trailing text (rounds after the last tool).
            _last_tool_ri = max(
                (
                    ri
                    for f in state.tc_frames
                    if (ri := _read_round_index(f.payload)) is not None
                ),
                default=-1,
            )
            joined = "".join(
                txt
                for ri, txt in sorted(_text_by_round.items())
                if ri > _last_tool_ri
            )
        else:
            joined = "".join(state.assistant_text_parts)
        if joined:
            fin = _TurnTailOutcome(
                frame_count=state.frame_count, synth_seq=state.synth_seq,
            )
            async for fr in self._finalize_assistant_message(
                conv=conv,
                tab=tab,
                request=request,
                now=now,
                joined=joined,
                final_parent_id=_final_parent_id,
                ttft_ms=state.ttft_ms,
                turn_started_ms=turn_started_ms,
                turn_usage=state.turn_usage,
                sub_agent_blocks=state.sub_agent_blocks,
                outcome=fin,
                round_request_ids=state.round_request_ids,
                last_round_usage=state.last_round_usage,
                first_round_usage=state.first_round_usage,
                # SUBAGENT-PER-ROUND-INSERT: baseline of the current turn's
                # tail in ``conv.messages`` — the per-round inserter scans
                # ONLY ``conv.messages[baseline:]`` for round messages so a
                # prior turn's ``round_index`` collision (if any) can never
                # be targeted.
                turn_persist_baseline=state.turn_persist_baseline,
            ):
                yield fr
            assistant_msg = fin.assistant_msg
            state.frame_count = fin.frame_count
            state.synth_seq = fin.synth_seq
        elif tool_call_msg is not None:
            # Tool-only turn (no final text): still save the tool messages.
            #
            # State-Truth-First (AGENTS.md 🟡🟡): a sub-agent dispatch round
            # that produced NO trailing assistant text must STILL persist its
            # ``sub_agent_blocks`` (the fold blocks the live stream already
            # rendered), exactly like the ``if joined:`` branch above and the
            # interrupt path (:meth:`_finalize_interrupted_turn`) do. Without
            # this, a turn where the model dispatched a sub-agent and then said
            # nothing saved only the ``[tool_calls]`` message WITHOUT the
            # blocks — so on a history reload the sub-agent tool cards vanished
            # ("工具卡跑完后消失") even though they streamed live.
            #
            # SUBAGENT-RELOAD-PERSIST-INDEPENDENT-MSG (2026-07-02): the fold
            # blocks used to be MERGED into ``tool_call_msg.meta.subAgentBlocks``
            # in place; on reload the sub-agent cards then rendered inside the
            # same message as the parent tool cards, with a template-field-
            # order dependency for their relative position. We now persist the
            # blocks on their OWN independent assistant message (built by
            # :meth:`_build_subagent_summary_message`), appended AFTER the
            # tool-call message so the DB timeline matches the live-stream
            # timeline. No-op when the turn folded no sub-agent block.
            # SUBAGENT-PER-ROUND-INSERT (2026-07-02): per-round independent
            # subagent_summary messages inserted after each dispatch round's
            # tool_call message, mirroring the live-stream shape. Fallback
            # (legacy / stub blocks without ``parent_round_index``) → single
            # end-of-turn append with ``parent_id=tool_call_msg.id``.
            self._insert_subagent_summary_messages_per_round(
                conv=conv,
                baseline=state.turn_persist_baseline,
                sub_agent_blocks=state.sub_agent_blocks,
                now=now,
                fallback_parent_id=tool_call_msg.id,
            )
            await self._save_parent_conv(
                conv, is_takeover=state.is_subagent_takeover
            )
        else:
            # ── W1-F / P2-a: empty response ERROR frame ─────────────────
            # When the model returns an empty response (no chunks with
            # text content), emit an ERROR frame so the frontend knows
            # the turn produced nothing, rather than silently ending.
            # V1 chat_handler.py:1767-1789: retryable=True + diagnostic.
            #
            # SKIP when the LLM stream already emitted a real error frame
            # (e.g. connect_error / timeout / HTTP 4xx) — emitting a
            # second ``empty_response`` would overwrite the real diagnostic
            # on the frontend, leaving the user with a useless message.
            if not state.had_llm_error:
                state.synth_seq += 1
                empty_err_frame = StreamFrame(
                    frame_id=f"empty-{state.synth_seq}",
                    frame_type=StreamFrameType.ERROR,
                    sequence=state.synth_seq,
                    payload={
                        "code": "empty_response",
                        "message": (
                            "模型返回了空响应。可能的原因：代理掩盖了上游错误 / "
                            "上游中断 / 模型静默拒绝。请重试或更换模型。"
                        ),
                        "retryable": True,
                    },
                )
                await self._publish(
                    ChatStreamFrameEvent(
                        tab_id=tab.id,
                        conversation_id=conv.id,
                        frame=empty_err_frame,
                    ),
                )
                yield empty_err_frame
                state.frame_count += 1

        # Re-insert mid-turn user injections (V2 enhancement) after the turn's
        # history tail was rebuilt above (the truncate-rebuild drops anything
        # past the baseline). Persist them so a reload + subsequent turns see
        # the injected ``role:user`` messages. Save only when there is
        # something to add (the branches above already saved the rebuilt tail).
        if state.injected_messages:
            self._reinsert_injected_messages(conv=conv, state=state)
            await self._save_parent_conv(
                conv, is_takeover=state.is_subagent_takeover
            )

        # ── V1 turn_warning emission ────────────────────────────────
        warn_outcome = _TurnTailOutcome(
            frame_count=state.frame_count, synth_seq=state.synth_seq,
        )
        async for fr in self._emit_turn_warning(
            conv=conv, tab=tab, outcome=warn_outcome,
        ):
            yield fr
        state.frame_count = warn_outcome.frame_count
        state.synth_seq = warn_outcome.synth_seq

        await self._complete_turn(
            tab=tab,
            conv=conv,
            now=now,
            frame_count=state.frame_count,
            assistant_msg=assistant_msg,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _compute_turn_warning_threshold(
        self,
        conversation_id: ConversationId,
        turn_count: int,
    ) -> int:
        """Return the threshold to warn at, or ``0`` for "no warning".

        Thin wrapper over
        :func:`_streaming_helpers.compute_turn_warning_threshold`
        (ARCH-1 / A-3 cohesion split): forwards the process-lifetime
        ``self._turn_warning_thresholds`` dict explicitly; logic
        byte-for-byte unchanged.
        """
        return _compute_turn_warning_threshold_fn(
            self._turn_warning_thresholds,
            conversation_id,
            turn_count,
        )

    async def _handle_abort(
        self,
        *,
        tab: Any,
        conv: Any,
        now: datetime,
        abort_reason: str,
        frame_count: int,
    ) -> None:
        """Finalise an aborted turn: mark tab ABORTED + publish event.

        Abort slice of :meth:`_run` (B1 cohesion split).  Byte-for-byte
        identical to the inline ``if aborted:`` branch.
        """
        tab.abort(now=now)
        await self._tabs.save(tab)
        await self._publish(
            ChatStreamAbortedEvent(
                tab_id=tab.id,
                conversation_id=conv.id,
                aborted_at=now,
                reason=abort_reason,
            ),
        )
        _log.info(
            "chat.stream_aborted",
            tab_id=tab.id.value,
            conversation_id=conv.id.value,
            reason=abort_reason,
            frames=frame_count,
        )

    async def _release_streaming_tab(
        self,
        *,
        tab: Any,
        now: datetime,
    ) -> None:
        """Defensively release a STREAMING tab back to a sendable IDLE.

        State-Truth-First failsafe (AGENTS.md 🔴 铁律5 — exception paths must
        be backstopped).  Called from the ``CancelledError`` and generic
        ``except Exception`` arms of :meth:`_run` so that a turn ending by
        *any* failure (provider crash, malformed on-device SSE frame, a bug
        in the follow-up loop, a refresh/disconnect) can NEVER leave the tab
        pinned in :data:`TabStatus.STREAMING`.  A pinned STREAMING tab makes
        every subsequent :meth:`ConversationTab.start_stream` raise
        ``requires status=IDLE, got streaming`` and the user's tab goes dead
        until a full reload.

        V1 parity (``useChat.js:2748`` ``finally`` unconditionally sets
        ``isStreaming=false`` → the input box re-enables regardless of how
        the turn ended).  We mirror that "back to sendable" semantics by
        transitioning STREAMING → IDLE (the normal-completion transition);
        the user-initiated Stop path keeps its distinct ABORTED terminal via
        :meth:`_handle_abort` and is therefore not routed through here.

        Idempotent + race-safe: only transitions when the tab is still
        STREAMING (the normal/abort exits may already have moved it), so a
        double-release or a tab already in IDLE/ABORTED/CLOSED is a no-op and
        never raises :class:`TabStateError`.
        """
        if not getattr(tab, "is_streaming", None) or not tab.is_streaming():
            return
        tab.complete_stream(now=now)
        await self._tabs.save(tab)
        _log.warning(
            "chat.tab_released_after_failure",
            tab_id=tab.id.value,
        )

    async def _persist_completed_rounds(
        self,
        *,
        conv: Any,
        now: datetime,
        state: "_TurnBodyState",
    ) -> None:
        """Incrementally persist the tool rounds completed SO FAR (idempotent).

        Root cause this fixes: V2 only wrote a turn's assistant / tool-round
        messages to the DB once the turn finished cleanly
        (:meth:`_finalize_turn`) or hit an ``except`` block
        (:meth:`_flush_tool_calls_on_interrupt`). A long agentic turn (e.g. 51
        tool calls over several minutes) whose backend process was simply
        killed / restarted / crashed mid-run -- so NO ``except`` ever ran --
        lost the entire reply, leaving the DB with just the user message (the
        reported "history shows only the user turn / nothing" bug). V1 never
        had this gap because its FRONTEND POSTed the full message list to the
        backend on every round / tab-switch / interrupt (``useChat.js:536``),
        so completed rounds were already durable.

        This restores that durability the V2 way -- server-side, every round,
        no front-end involvement (judge 1: persistence is a backend
        responsibility; judge 2: behaviour matches V1 "completed work is never
        lost"). It is called from the agentic loop whenever a round's tool
        results have been collected.

        Idempotency (so calling it every round does NOT stack duplicate
        messages): the turn's own messages always live at
        ``conv.messages[baseline:]`` (``baseline`` = index just past the user
        message). We truncate back to ``baseline`` and rebuild the consolidated
        per-round ASSISTANT tool messages from the current ``tc_frames`` /
        ``tr_frames`` via :meth:`_build_tool_call_message` (the same builder
        :meth:`_finalize_turn` uses), then save. No-op until the baseline is
        recorded and at least one tool frame exists.

        ``user_msg`` (the parent the rebuilt round messages chain under) is
        derived from ``conv`` itself -- the message at ``baseline - 1`` (the
        turn's freshly appended user message) -- so this method needs no
        ``user_msg`` parameter and works from any call site that only has
        ``conv`` in scope (e.g. the ``_drain_main_stream`` /
        ``_handle_initial_tool_call`` slices).
        """
        baseline = state.turn_persist_baseline
        if baseline <= 0 or not state.tc_frames:
            return
        user_msg = conv.messages[baseline - 1]
        # Idempotent rebuild: drop whatever this turn appended last time, then
        # re-derive the per-round tool messages from the accumulated frames.
        if len(conv.messages) > baseline:
            del conv.messages[baseline:]
        tool_call_msg = self._build_tool_call_message(
            now=now,
            user_msg=user_msg,
            conv=conv,
            tc_frames=state.tc_frames,
            tr_frames=state.tr_frames,
        )
        if tool_call_msg is None:
            return
        # SUBAGENT-PER-ROUND-INSERT (2026-07-02) — durability兜底: the truncate
        # above drops any subagent_summary messages from a prior incremental
        # save, so they MUST be rebuilt here alongside the tool_call rounds.
        # Without this, an incremental save that crashes / gets killed mid-run
        # (the very scenario :meth:`_persist_completed_rounds` was introduced
        # to defend against — a long agentic turn losing its work) would leave
        # the DB with the per-round tool messages but WITHOUT the sub-agent
        # panels the user saw live (the "sub-agent panel disappeared after
        # incremental persist crash" regression). Idempotent: the truncate
        # above already dropped the prior copies, so re-inserting from
        # ``state.sub_agent_blocks`` never stacks duplicates.
        self._insert_subagent_summary_messages_per_round(
            conv=conv,
            baseline=baseline,
            sub_agent_blocks=state.sub_agent_blocks,
            now=now,
            fallback_parent_id=tool_call_msg.id,
        )
        # Re-insert mid-turn user injections (V2 enhancement) AFTER the rebuild
        # so the truncate above does not drop them (they live past the
        # baseline). Idempotent: always re-derived from ``state`` (the next
        # incremental save truncates + re-appends them again, no stacking).
        self._reinsert_injected_messages(conv=conv, state=state)
        await self._save_parent_conv(
            conv, is_takeover=state.is_subagent_takeover
        )

    def _reinsert_injected_messages(
        self,
        *,
        conv: Any,
        state: "_TurnBodyState",
    ) -> None:
        """Re-insert this turn's mid-turn user injections at their round seams.

        The turn's history tail is rebuilt from frames on every persist (the
        ``del conv.messages[baseline:]`` + ``build_tool_call_message`` path),
        which drops anything appended directly to ``conv`` past the baseline.
        A mid-turn user injection (the "inject" button, V2 enhancement) is a
        ``role:user`` message that must survive that rebuild AND be visible to
        subsequent turns' history wire, so the run loop records each minted
        :class:`Message` in ``state.injected_messages`` (as ``(round_no,
        Message)``) instead of appending it to ``conv`` directly, and this
        method re-inserts them after every rebuild.

        Positional ordering: each injection's ``round_no`` is the agentic-loop
        round it was folded BEFORE (the inter-round gap between round
        ``round_no-1``'s tools and round ``round_no``'s LLM call). The rebuilt
        turn tail lays out one assistant message per round in round order
        (each stamped with ``meta.round_index`` == its kernel ``round_no`` by
        :func:`build_tool_call_message`) followed by a final assistant text
        message. We therefore insert an injection with ``round_no == R``
        immediately BEFORE the first rebuilt turn-tail message whose
        ``meta.round_index >= R`` (i.e. after every round < R, before round R),
        falling back to just before the final assistant text message (or the
        very end) when no such round message exists (the injection happened in
        the gap before a round that never produced tool cards / the final
        round). This makes the persisted (reload) order match the live
        inter-round order exactly — the user's mid-turn instruction reloads at
        the same position it steered the model from, not at the turn's end.

        Multiple injections sharing the same ``round_no`` gap keep arrival
        (FIFO) order: ``state.injected_messages`` is already in arrival order,
        and inserting consecutively at the same computed index preserves it.

        Idempotent: re-deriving from ``state.injected_messages`` each call means
        repeated incremental saves never stack duplicates (the preceding
        truncate already removed the prior copies). Each message keeps its
        originally-minted id (stable for the ``injected_message`` frame's
        ``message_id`` pairing); only its ``parent_id`` is re-chained to the
        message it now follows so the linked-list order stays consistent with
        the new array order.
        """
        if not state.injected_messages:
            return
        # The turn tail starts at the persist baseline (the index just past the
        # turn's user message). Injections are positioned WITHIN that tail; we
        # never disturb prior turns. ``baseline`` may be 0 / unset on the unit
        # transform — clamp to a safe range over the current array.
        baseline = state.turn_persist_baseline
        if not isinstance(baseline, int) or baseline < 0:
            baseline = 0
        for round_no, injected_msg in state.injected_messages:
            insert_at = StreamChatUseCase._injection_insert_index(
                conv=conv, baseline=baseline, round_no=round_no,
            )
            # Re-chain parent under the message we are inserting AFTER so the
            # linked-list order matches the new array order. The message keeps
            # its originally-minted id (stable for the ``injected_message``
            # frame's ``message_id`` pairing).
            prev = conv.messages[insert_at - 1] if insert_at > 0 else None
            rechained = (
                replace(injected_msg, parent_id=prev.id)
                if prev is not None
                else injected_msg
            )
            conv.messages.insert(insert_at, rechained)

    @staticmethod
    def _injection_insert_index(
        *,
        conv: Any,
        baseline: int,
        round_no: int,
    ) -> int:
        """Index at which to insert an injection recorded with ``round_no``.

        Walks the rebuilt turn tail (``conv.messages[baseline:]``) and returns
        the index of the first message whose ``meta.round_index >= round_no``
        (insert BEFORE it). When no such round message exists (the injection
        belongs to the gap before the final / a card-less round), returns the
        end of the array so the injection lands just after the last round
        message and before nothing — i.e. at the tail, which is correct for an
        injection that has no later per-round message to precede.
        """
        messages = conv.messages
        n = len(messages)
        start = baseline if 0 <= baseline <= n else 0
        for idx in range(start, n):
            meta = getattr(messages[idx], "meta", None)
            if not isinstance(meta, dict):
                continue
            ri = meta.get("round_index")
            if (
                isinstance(ri, int)
                and not isinstance(ri, bool)
                and ri >= round_no
            ):
                return idx
        return n

    async def _persist_injections_on_interrupt(
        self,
        *,
        conv: Any,
        state: "_TurnBodyState",
    ) -> None:
        """Durably persist mid-turn user injections on an interrupt/abort path.

        State-Truth-First (AGENTS.md 🔴 铁律3 + 🟡🟡 发现缺陷必须修): a mid-turn
        user injection is recorded in ``state.injected_messages`` by the run
        loop and is only written to ``conv`` by :meth:`_reinsert_injected_messages`
        on the NORMAL persist sites (:meth:`_finalize_turn` /
        :meth:`_persist_completed_rounds`). The interrupt/abort termination
        paths (CancelledError on refresh/disconnect, GeneratorExit on consumer
        ``aclose()``, the abort path on user Stop) previously did NOT re-insert,
        and the ``finally`` then cleared the injection registry — so an
        injection made in the round that got interrupted BEFORE any normal
        persist ran was PERMANENTLY LOST from the DB even though the model had
        already seen it on the wire (the injection is control-plane only; the
        composer cleared the draft and there is NO frontend re-send queue for
        it). This re-inserts the injections at their round seams and saves the
        conversation, mirroring the save pattern the interrupt path already
        uses for tool rounds, so the injection is durable BEFORE the registry
        clear. No-op (and no save) when nothing was injected this turn.

        ``_reinsert_injected_messages`` is sync; only the save is awaited — the
        same shape as :meth:`_flush_tool_calls_on_interrupt`, so it is safe on
        every interrupt path that already awaits a save (incl. the
        ``GeneratorExit`` path, which already awaits ``_flush`` + a save).
        """
        if not state.injected_messages:
            return
        self._reinsert_injected_messages(conv=conv, state=state)
        await self._save_parent_conv(
            conv, is_takeover=state.is_subagent_takeover
        )
        _log.info(
            "chat.injections_persisted_on_interrupt",
            conversation_id=conv.id.value,
            injected=len(state.injected_messages),
        )

    async def _flush_tool_calls_on_interrupt(
        self,
        *,
        conv: Any,
        user_msg: Message,
        now: datetime,
        state: "_TurnBodyState",
    ) -> Message | None:
        """Persist already-completed tool rounds when a turn is interrupted.

        V1 PARITY (frontend ``useChat.js:2747-2771`` ``finally`` save): V1
        kept every completed tool round in ``messages.value`` and saved the
        whole array in ``finally``, so a mid-task abort / refresh / error
        never lost the tool-call history. V2 persists server-side and only
        did so in :meth:`_finalize_turn` (the normal-completion path), so any
        interruption (user Stop, SSE disconnect on refresh, or a mid-loop
        exception) dropped every accumulated tool round, leaving the DB with
        just the user message ("刷新后工具调用历史消失、只剩 1 轮").

        This flush builds the same consolidated ASSISTANT tool-call message
        :meth:`_finalize_turn` would have built (via
        :meth:`_build_tool_call_message`, which appends it to ``conv``) and
        persists the conversation, so the completed rounds survive the
        interruption. No-op when no tool rounds have run yet.

        Returns the LAST persisted tool-round message (used by
        :meth:`_persist_interrupted_assistant_message` to chain / retag the
        interrupt marker), or ``None`` when no tool rounds were collected.

        Idempotent with :meth:`_persist_completed_rounds`: incremental
        per-round persistence may already have written this turn's tool
        messages to ``conv``. We therefore truncate ``conv.messages`` back to
        the turn baseline before rebuilding, so an interrupt that fires after
        one or more incremental saves does not stack duplicate tool messages.
        """
        if not state.tc_frames:
            return None
        _baseline = state.turn_persist_baseline
        if _baseline >= 0 and len(conv.messages) > _baseline:
            del conv.messages[_baseline:]
        tool_call_msg = self._build_tool_call_message(
            now=now,
            user_msg=user_msg,
            conv=conv,
            tc_frames=state.tc_frames,
            tr_frames=state.tr_frames,
        )
        if tool_call_msg is None:
            return None
        # SUBAGENT-PER-ROUND-INSERT (2026-07-02) — durability兜底: the truncate
        # above drops any subagent_summary messages the incremental save may
        # have already inserted, so rebuild them here. Not all callers of
        # this flush go on to call
        # :meth:`_persist_interrupted_assistant_message` (the GeneratorExit /
        # generic Exception paths save right after this flush and return),
        # so without rebuilding here the sub-agent panels the user saw live
        # would vanish from the DB even though the tool_call rows survived.
        # The ``_persist_interrupted_assistant_message`` path that DOES run
        # afterwards also calls the per-round inserter, but by then the
        # subagent_summary messages we insert here are already present + the
        # inserter is idempotent on the current ``sub_agent_blocks`` (its
        # rebuild path truncates the tail first) — no double insertion.
        # However, the two paths currently coexist on DIFFERENT truncate
        # boundaries (this flush truncates the whole tail; the interrupt
        # persist path does NOT truncate), so the safer contract is: this
        # flush is the ONLY place the tail is truncated on the interrupt
        # path; subsequent :meth:`_persist_interrupted_assistant_message`
        # calls just append the trailing-text / retag path AFTER us.
        self._insert_subagent_summary_messages_per_round(
            conv=conv,
            baseline=_baseline,
            sub_agent_blocks=state.sub_agent_blocks,
            now=now,
            fallback_parent_id=tool_call_msg.id,
        )
        await self._save_parent_conv(
            conv, is_takeover=state.is_subagent_takeover
        )
        _log.info(
            "chat.tool_calls_flushed_on_interrupt",
            conversation_id=conv.id.value,
            tool_rounds=len(state.tc_frames),
        )
        return tool_call_msg

    async def _persist_interrupted_assistant_message(
        self,
        *,
        conv: Any,
        user_msg: Message,
        now: datetime,
        state: "_TurnBodyState",
        last_tool_msg: Message | None,
    ) -> None:
        """Persist the user-visible ``meta.interrupted=True`` marker.

        V1 PARITY (``useChat.js:2688-2712``): when a turn is aborted (user
        Stop / SSE disconnect on refresh), V1 committed the partial assistant
        output as an assistant message tagged with the interrupt marker so a
        history reload always shows that the turn was interrupted. V2 renders
        the marker off ``meta.interrupted === true`` (ChatMessageList.vue
        :329-334 + historyMapper.ts preserves it) — a cleaner envelope than
        V1's content-string ``"\n\n*[Interrupted]*"`` (judge 1: doesn't
        pollute ``content``), but the persistence was missing, so the marker
        vanished on reload while the tool cards stayed.

        Three cases (the per-round ``assistant_text_parts.clear()`` at the top
        of each follow-up round means the parts hold ONLY the trailing,
        not-yet-folded text of the round that was streaming when the abort
        landed):

        * **trailing text present** (model spoke after the last tool round) →
          append a NEW assistant message carrying that text +
          ``meta.interrupted=True``, chained under ``last_tool_msg`` (or the
          user message when no tool rounds ran);
        * **no trailing text but tool rounds ran** → retag ``last_tool_msg``
          in place (``dataclasses.replace`` — Message is frozen) so its meta
          carries ``interrupted=True`` while PRESERVING any existing meta keys
          (e.g. the round's ``request_id``); the marker then renders beneath
          that round's lead-in;
        * **nothing was said and no tool rounds** → no-op (the domain forbids
          empty ``MessageContent.text`` and the frontend interrupt bubble
          requires non-empty content anyway).
        """
        trailing = "".join(state.assistant_text_parts).strip()
        # Resolve the prompt-snapshot request_id for this interrupted turn,
        # reusing the SAME selection as the normal-completion path
        # (:meth:`_finalize_assistant_message`): the highest-index round's id
        # (the round that produced the trailing text).  The snapshot itself
        # was already saved when the first frame arrived
        # (:meth:`_capture_round_zero_snapshot`), so the id exists even though
        # the turn was cut short — we just need to carry it onto the message
        # so the 📄 "view prompt" button survives a history reload (user
        # 2026-06-15: interrupt must still expose the prompt snapshot; this is
        # a deliberate enhancement over V1, which dropped the id on abort).
        _interrupt_request_id: str | None = None
        if state.round_request_ids:
            _interrupt_request_id = state.round_request_ids.get(
                max(state.round_request_ids)
            )
        if trailing:
            _parent_id = (
                last_tool_msg.id if last_tool_msg is not None
                else (conv.last_message().id if conv.last_message() else None)
            )
            _meta: dict[str, Any] = {"interrupted": True}
            if _interrupt_request_id is not None:
                _meta["request_id"] = _interrupt_request_id
            # SUBAGENT-PER-ROUND-INSERT (2026-07-02): sub-agent panels are
            # already inserted by :meth:`_flush_tool_calls_on_interrupt`
            # (called by every interrupt path right before this method), so
            # we DO NOT re-insert here — the flush truncates the whole turn
            # tail and rebuilds tool_call rounds + per-round subagent_summary
            # in one shot. Doing it a second time would duplicate every
            # sub-agent panel on the reload transcript. The trailing-text
            # interrupt message is simply appended AFTER the last inserted
            # subagent_summary (natural end-of-tail append order = correct
            # visual order).
            interrupted_msg = self._build_message(
                role=MessageRole.ASSISTANT,
                content=MessageContent(text=trailing),
                now=now,
                parent_id=_parent_id,
                meta=_meta,
            )
            conv.append_message(interrupted_msg)
            await self._save_parent_conv(
                conv, is_takeover=state.is_subagent_takeover
            )
            return
        if last_tool_msg is not None:
            # Retag the last tool-round message in place (frozen → replace).
            # Merge — never overwrite — its existing meta so the round's
            # ``request_id`` (📄 prompt-snapshot button) survives.
            merged_meta = dict(last_tool_msg.meta or {})
            merged_meta["interrupted"] = True
            retagged = replace(last_tool_msg, meta=merged_meta)
            for i, m in enumerate(conv.messages):
                if m.id == last_tool_msg.id:
                    conv.messages[i] = retagged
                    break
            # SUBAGENT-PER-ROUND-INSERT (2026-07-02): sub-agent panels are
            # already inserted by :meth:`_flush_tool_calls_on_interrupt` (see
            # the trailing-text branch above for the full rationale). No
            # re-insertion here; the retag above operates on the round message
            # in place and leaves the subagent_summary messages (already
            # inserted after each dispatch round) untouched.
            await self._save_parent_conv(
                conv, is_takeover=state.is_subagent_takeover
            )
            return
        # Nothing said + no tool rounds → no-op (empty content is invalid).

    def _build_tool_call_message(
        self,
        *,
        now: datetime,
        user_msg: Message,
        conv: Any,
        tc_frames: list[StreamFrame],
        tr_frames: list[StreamFrame],
        text_by_round: dict[int, str] | None = None,
    ) -> Message | None:
        """Persist the agentic-loop tool calls as one ASSISTANT message.

        Thin wrapper over
        :func:`_streaming_helpers.build_tool_call_message` (ARCH-1 / A-3
        cohesion split): forwards ``self._ids`` explicitly; logic
        byte-for-byte unchanged (still appends to ``conv``). ``text_by_round``
        (self-contained agent only) interleaves per-round answer text with the
        tool rounds so a reload matches the live order.
        """
        return _build_tool_call_message_fn(
            self._ids,
            now=now,
            user_msg=user_msg,
            conv=conv,
            tc_frames=tc_frames,
            tr_frames=tr_frames,
            text_by_round=text_by_round,
        )

    async def _update_full_history_counter(
        self,
        *,
        conv: Any,
        eff_prompt: int,
        completion_tokens: int,
        model_hint: str,
    ) -> None:
        """Advance ``conv.full_history_tokens`` from one turn's measured wire.

        SHARED口径 between the normal-completion finalize
        (:meth:`_finalize_assistant_message`) and the three interrupt paths
        (CancelledError / Exception / abort, via
        :meth:`_update_full_history_counter_on_interrupt`). Extracting it here
        (B-cohesion split) removes the "only the happy path keeps the running
        history counter" asymmetry that left the badge frozen on a mid-turn
        interruption (AGENTS.md 🔴 State-Truth-First 铁律 5: 异常退出路径必须
        兜底 + 铁律 1: 缓存脱节必须纠偏).

        ``eff_prompt`` is the provider-measured effective prompt size of the
        turn's LAST round (``assistant_eff_prompt``口径 — adds the Anthropic
        cache-read split-out back). ``completion_tokens`` is the same round's
        completion. Both must be computed by the caller with the SAME formula
        the normal path used so the two records stay byte-for-byte equivalent.

        Semantics (identical to the prior inline block at the old
        ``_finalize_assistant_message`` lines):

        * NO compaction checkpoint → ``full_history_tokens = eff_prompt +
          completion`` (the full history IS the wire).
        * checkpoint EXISTS → post-compaction DELTA growth: seed
          ``ckpt.last_eff_prompt`` on the first post-compaction turn (no delta
          that turn — char ``est``口径 ≠ cloud measurement), then GROW by the
          positive delta of consecutive cloud readings + this turn's
          completion. CCD-5/TPP-1 fix: once ``ckpt.last_eff_prompt`` advances we
          WRITE IT THROUGH to the durable checkpoint store so a restart /
          repeated interrupt no longer re-seeds (delta=0) and freezes the
          counter (the previous "do not persist last_eff_prompt for perf"
          stance caused the long-term-freeze bug — persisting one extra INTEGER
          column on the already-written checkpoint row is cheap, and the save
          is best-effort so it never breaks the turn).

        ``eff_prompt <= 0`` is a no-op (no measurement this turn — e.g. a
        local model that emitted no usage): we keep the prior value rather than
        writing 0, so the counter is never dragged DOWN below the real history
        (AGENTS.md State-Truth-First: don't replace truth with a false 0).
        """
        if eff_prompt <= 0:
            return
        ckpt = self._compaction_checkpoints.get(self._conv_key(conv))
        _fht_before = getattr(conv, "full_history_tokens", None)
        _log.info(
            "chat.diag.full_history_update_in",
            conversation_id=conv.id.value,
            model_id=model_hint,
            is_anthropic=_is_anthropic_family(model_hint),
            eff_prompt=eff_prompt,
            completion=completion_tokens,
            full_history_before=_fht_before,
            has_checkpoint=ckpt is not None,
            ckpt_last_eff_prompt=(
                ckpt.last_eff_prompt if ckpt is not None else None
            ),
            ckpt_estimated_tokens=(
                ckpt.estimated_tokens if ckpt is not None else None
            ),
        )
        if ckpt is None:
            conv.full_history_tokens = eff_prompt + completion_tokens
        else:
            _ckpt_changed = False
            if ckpt.last_eff_prompt is None:
                # First post-compaction turn: seed baseline, no delta this turn
                # (char ``est`` at compaction creation is a different口径 from
                # the cloud measurement).
                ckpt.last_eff_prompt = eff_prompt
                _ckpt_changed = True
            else:
                delta = eff_prompt - ckpt.last_eff_prompt
                if delta < 0:
                    delta = 0  # guard against noise / reordering
                if conv.full_history_tokens is None:
                    # Never-measured conversation that nonetheless got compacted
                    # (e.g. a long local-model history whose chars/4 trigger
                    # fired while usage stayed 0). Seed the counter from this
                    # turn's measured size rather than growing a delta up from 0
                    # (which would badly undercount the frozen full history).
                    conv.full_history_tokens = eff_prompt + completion_tokens
                else:
                    conv.full_history_tokens = (
                        int(conv.full_history_tokens) + delta + completion_tokens
                    )
                if eff_prompt != ckpt.last_eff_prompt:
                    ckpt.last_eff_prompt = eff_prompt
                    _ckpt_changed = True
            # CCD-5/TPP-1 (persist the delta baseline): write the advanced
            # ``last_eff_prompt`` through to the durable store. Earlier this was
            # deliberately NOT persisted ("cheap + re-derivable") — but that let
            # the counter FREEZE after a restart / repeated mid-turn interrupt
            # (the first post-compaction turn always re-seeded delta=0,
            # never growing). Persisting one INTEGER on the already-existing
            # checkpoint row fixes the freeze; best-effort so it never breaks
            # the turn (AGENTS.md State-Truth-First 铁律 1: real state must
            # survive restart, not silently revert to a stale baseline).
            if _ckpt_changed:
                try:
                    await self._persist_checkpoint(conv, ckpt)
                except Exception:  # noqa: BLE001 — never break persistence
                    pass
        _log.info(
            "chat.diag.full_history_update_out",
            conversation_id=conv.id.value,
            full_history_after=getattr(conv, "full_history_tokens", None),
        )

    async def _update_full_history_counter_on_interrupt(
        self,
        *,
        conv: Any,
        state: "_TurnBodyState",
        model_hint: str,
    ) -> None:
        """Interrupt-path bridge to :meth:`_update_full_history_counter`.

        The three interrupt paths (CancelledError refresh/disconnect, mid-loop
        Exception, user-Stop abort) persist the partial turn into ``conv``
        (DB grows) but historically NEVER advanced ``conv.full_history_tokens``
        — so after the user reloads, GET ``/context`` read the STALE counter and
        the badge stayed frozen at the pre-interrupt value,脱节 from the real
        wire the model actually received (AGENTS.md 🔴 State-Truth-First 铁律 5
        异常路径未兜底 + 铁律 1 缓存脱节).

        ``state.last_round_usage`` is the RAW last-round usage dict captured by
        ``_on_round_end`` (keys ``prompt_tokens`` / ``cache_read_tokens`` /
        ``completion_tokens``). It does NOT carry the ``last_round_*`` tail keys
        the normal path appends in ``_finalize_assistant_message`` — so we MAP
        them here (``prompt_tokens`` → ``last_round_prompt_tokens``,
        ``cache_read_tokens`` → ``last_round_cache_read_tokens``) and feed the
        SAME ``assistant_eff_prompt`` formula, keeping the interrupt record口径-
        identical to the happy path (覆盖/累加 semantics are decided inside
        :meth:`_update_full_history_counter`: it GROWS the counter by this
        turn's delta exactly as the normal path does, no double-count).

        No-op when no round usage exists (local / no-usage turn) — the counter
        keeps its prior value rather than being written to 0 (don't误写小).
        """
        lru = state.last_round_usage
        if not isinstance(lru, dict):
            return
        try:
            _pt = int(lru.get("prompt_tokens") or 0)
            _cr = int(lru.get("cache_read_tokens") or 0)
            _comp = int(lru.get("completion_tokens") or 0)
        except (TypeError, ValueError):
            return
        if _pt <= 0:
            return
        _synth_usage = {
            "last_round_prompt_tokens": _pt,
            "last_round_cache_read_tokens": _cr,
        }
        _eff_prompt = _assistant_eff_prompt(
            _synth_usage, _is_anthropic_family(model_hint)
        )
        _before = getattr(conv, "full_history_tokens", None)
        try:
            await self._update_full_history_counter(
                conv=conv,
                eff_prompt=_eff_prompt,
                completion_tokens=_comp,
                model_hint=model_hint,
            )
        except Exception:  # noqa: BLE001 — never break the interrupt flush
            return
        # Durably persist the advanced counter: the interrupt paths already
        # ``save_messages(conv)`` BEFORE this counter update (when flushing the
        # partial turn), so without a fresh save here the in-memory counter bump
        # would be lost on reload — defeating the whole兜底. Save only when the
        # value actually changed (avoid a redundant write on a no-op turn).
        _after = getattr(conv, "full_history_tokens", None)
        if _after != _before:
            try:
                await self._save_parent_conv(
                    conv, is_takeover=state.is_subagent_takeover
                )
            except Exception:  # noqa: BLE001 — never break the interrupt flush
                pass

    async def _finalize_assistant_message(
        self,
        *,
        conv: Any,
        tab: Any,
        request: "StreamChatInput",
        now: datetime,
        joined: str,
        final_parent_id: MessageId | None,
        ttft_ms: int | None,
        turn_started_ms: int,
        turn_usage: dict[str, Any] | None,
        sub_agent_blocks: dict[Any, dict[str, Any]],
        outcome: "_TurnTailOutcome",
        round_request_ids: dict[int, str] | None = None,
        last_round_usage: dict[str, Any] | None = None,
        first_round_usage: dict[str, Any] | None = None,
        turn_persist_baseline: int = -1,
    ) -> AsyncIterator[StreamFrame]:
        """Persist the final assistant message; yield the snapshot END frame.

        Assistant-finalize slice of :meth:`_run` (B1 cohesion split),
        byte-for-byte identical to the inline ``if joined:`` branch:

        * resolve the prompt-snapshot ``request_id`` for the final text
          message: when per-round snapshots were saved (``round_request_ids``
          non-empty), use the LAST round's id (the round that produced the
          final text) — V1 parity (``useChat.js:2402``: the final assistant
          text message carries the last ``/api/chat`` round's ``request_id``).
          Fall back to saving a turn-level snapshot when no per-round ids
          exist (legacy / snapshot store not wired);
        * stamp the producing model id / provider (V1 useModels.js:53-70);
        * build the V1-parity meta envelope (request_id / perf /
          subAgentBlocks);
        * append + persist the assistant message + publish
          :class:`MessageAppendedEvent`;
        * when a snapshot id was produced, yield a supplementary
          ``end`` frame (``reason="snapshot"`` + ``request_id``) so the
          "Prompt Snapshot" button surfaces live (V1
          ``backend/main.py:6716-6720``).

        Records the finalised message + advanced counters into ``outcome``.
        """
        frame_count = outcome.frame_count
        synth_seq = outcome.synth_seq
        # Per-round snapshot (V1 parity): the final text message carries the
        # LAST round's request_id (the round that produced the text).  When
        # per-round snapshots were saved, pick the highest round index; else
        # fall back to saving a turn-level snapshot (legacy / no store).
        _snapshot_request_id: str | None = None
        if round_request_ids:
            # Use the last (highest-index) round's id.
            _snapshot_request_id = round_request_ids.get(
                max(round_request_ids)
            )
        if _snapshot_request_id is None:
            # Fallback: no per-round ids (snapshot store not wired, or
            # non-agentic turn with no followup loop) — save a turn-level
            # snapshot as before.
            _snapshot_request_id = await self._save_prompt_snapshot(
                conv=conv, request=request, model_hint=request.model_hint,
            )
        # ``model_hint`` is the selected model id; provider is carried in
        # ``extra["tool_params"]["selected_model_provider"]`` when the
        # client supplies it (else None — id-only lookup still resolves).
        _tp = (request.extra or {}).get("tool_params") if request.extra else None
        _provider = (
            _tp.get("selected_model_provider")
            if isinstance(_tp, dict)
            else None
        )
        _assistant_meta = _build_assistant_meta(
            request_id=_snapshot_request_id,
            ttft_ms=ttft_ms,
            turn_started_ms=turn_started_ms,
            now_ms=_now_ms(self._clock),
            usage=turn_usage,
            # SUBAGENT-RELOAD-PERSIST-INDEPENDENT-MSG (2026-07-02): the fold
            # blocks are NO LONGER inlined into the final-summary message's
            # meta; they live on their OWN dedicated ``subagent_summary``
            # message appended just below. Pass an empty dict so
            # ``build_assistant_meta`` does not add ``subAgentBlocks`` here.
            # ``build_assistant_meta`` itself keeps the legacy parameter (its
            # unit test still exercises the sorted-by-index path) so
            # backward-compat reads of old messages are unaffected.
            sub_agent_blocks={},
        )
        # Tail-append the keystone last-round figures to the persisted usage so
        # a reload (and the running full-history counter below) can read the
        # LAST round's true wire prompt size explicitly. NOTE: as of the Bug-B
        # fix, ``turn_usage.prompt_tokens`` is ALREADY corrected to the last
        # round's value for anthropic-family models (``_finalize_turn_usage`` at
        # the END-frame assembly), so it is no longer the bogus cross-round SUM;
        # non-anthropic providers keep their original per-round口径. These
        # explicit ``last_round_*`` keys remain the unambiguous keystone the
        # full-history counter / ``assistant_eff_prompt`` prefer. Single-round
        # turns: last_round == accumulated (no display behaviour change).
        # AGENTS.md §3.1 tail-only.
        if turn_usage is not None and last_round_usage is not None:
            # SHARED tail-append口径 (DRY) — the exact same helper the live
            # END-frame path calls, so the persisted-message usage and the
            # END-frame usage carry byte-identical ``last_round_*`` /
            # display fields (fixes the live-vs-reload ↑ drift where the live
            # badge summed the whole prompt because the END frame lacked
            # ``last_round_cache_read_display``). DISPLAY-ONLY fields; counter /
            # ``assistant_eff_prompt`` never read them, and ``cache_read_tokens``
            # stays whatever ``_extract_usage`` set (zeroed on a cache-hit turn
            # to protect billing double-add). See _append_display_usage_fields.
            turn_usage = self._append_display_usage_fields(
                turn_usage, last_round_usage, first_round_usage
            )
            # DIAG (token-display investigation): the keystone figure that later
            # drives "发给模型 ~7.1K". ``last_round_prompt_tokens`` here is the
            # LAST round's _extract_usage-corrected prompt; if it is tiny on a
            # long compacted history, the badge "compacted" number collapses.
            # Surface the per-round vs the cross-round SUM so we can tell which
            # the model actually received. Remove once root-caused.
            _log.info(
                "chat.diag.tail_append_usage",
                conversation_id=conv.id.value,
                last_round_usage=dict(last_round_usage),
                turn_usage_prompt_tokens=turn_usage.get("prompt_tokens"),
                turn_usage_total_tokens=turn_usage.get("total_tokens"),
                last_round_prompt_tokens=turn_usage.get(
                    "last_round_prompt_tokens"
                ),
                last_round_cache_read_tokens=turn_usage.get(
                    "last_round_cache_read_tokens"
                ),
            )
        # SUBAGENT-PER-ROUND-INSERT (2026-07-02): if the turn dispatched any
        # sub-agents, persist one INDEPENDENT ``subagent_summary`` message
        # PER dispatch round, inserted IMMEDIATELY AFTER the parent round
        # message that dispatched those sub-agents. See
        # :meth:`_insert_subagent_summary_messages_per_round` for the exact
        # per-round grouping + insertion contract; fallback for legacy blocks
        # lacking ``parent_round_index`` (or whose target round message is
        # missing from the turn tail) is a single end-of-turn append,
        # byte-for-byte pre-fix behaviour.
        #
        # Live-stream shape:
        #   ...round R msg (agent-dispatch card) → subagent_summary
        #     (round R's sub-agents) → round R+1 msg (next tool) →
        #     ... → final assistant text msg
        # This helper reproduces that on reload (fixes the "sub-agent panel
        # piled at end of turn instead of next to the round that dispatched
        # it" bug — the previously observed
        # ``agent card → text → read card → text → subagent panel → ↩``
        # order on reload while the live stream had shown
        # ``agent card → subagent panel → text → read card → ↩``).
        #
        # ``final_parent_id`` remains pointed at the LAST tool-call round
        # message (or the user message when no tool rounds ran) — parent_id
        # is a DAG link used only for branching, NOT for ordering (order
        # comes from ``chat_message.position`` = list-append index —
        # ``conversation_repository.py:98``), so keeping it pointed at
        # tool_call_msg avoids threading it through the per-round summaries.
        self._insert_subagent_summary_messages_per_round(
            conv=conv,
            baseline=turn_persist_baseline,
            sub_agent_blocks=sub_agent_blocks,
            now=now,
            fallback_parent_id=final_parent_id,
        )
        assistant_msg = self._build_message(
            role=MessageRole.ASSISTANT,
            content=MessageContent(text=joined),
            now=now,
            parent_id=final_parent_id,
            usage=turn_usage,
            model_id=request.model_hint or None,
            model_provider=_provider if isinstance(_provider, str) else None,
            meta=_assistant_meta,
        )
        conv.append_message(assistant_msg)
        # Running full-history token counter (provider-measured). The whole
        # update — checkpoint-aware delta growth + last_eff_prompt write-through
        # — now lives in the SHARED :meth:`_update_full_history_counter` so the
        # interrupt paths can reuse the EXACT same口径 (B-cohesion split; removes
        # the "only the happy path keeps the counter" asymmetry). Behaviour here
        # is byte-for-byte the prior inline block.
        try:
            if turn_usage is not None:
                _comp = int(turn_usage.get("completion_tokens") or 0)
                # provider-family cache branching: Claude splits cache reads out
                # of prompt_tokens, so eff_prompt must add them back; OpenAI /
                # Azure / Gemini / Vertex already fold cache into prompt_tokens.
                # Shared口径 via ``assistant_eff_prompt`` so the badge counter
                # and the compaction-trigger ``实发`` use ONE formula.
                # CCD-2 (PENDING-WORK.md §1): ``turn_usage`` is the JUST-
                # FINALISED current round's usage, so its source model IS the
                # current request's model — ``request.model_hint`` is the
                # correct family discriminator here (no model-switch ambiguity).
                _model_id = request.model_hint or ""
                _eff_prompt = _assistant_eff_prompt(
                    turn_usage, _is_anthropic_family(_model_id)
                )
                await self._update_full_history_counter(
                    conv=conv,
                    eff_prompt=_eff_prompt,
                    completion_tokens=_comp,
                    model_hint=_model_id,
                )
        except Exception:  # noqa: BLE001 - never break persistence
            pass
        # Promote-ready turn-end detection (migration 057): extract the model
        # workspace path from THIS turn's final summary text, scan it for
        # promote-eligible precision variants, and stash the result on
        # ``conv.detected_model`` so the ``save_messages`` below persists it
        # (both ON CONFLICT branches write ``detected_model_json``). The
        # frontend CTA then reads it with ZERO on-open disk scans. Runs BEFORE
        # ``_save_parent_conv`` so the write is durable in the same save. Fully
        # best-effort: any failure is swallowed so a bookkeeping hiccup never
        # breaks turn completion (AGENTS.md §5).
        await self._detect_promote_ready(conv=conv, joined=joined)
        await self._save_parent_conv(
            conv, is_takeover=self._is_subagent_takeover_turn(request)
        )
        await self._publish(
            MessageAppendedEvent(
                conversation_id=conv.id,
                message_id=assistant_msg.id,
                role=assistant_msg.role.value,
                appended_at=assistant_msg.created_at,
            ),
        )
        if _snapshot_request_id is not None:
            synth_seq += 1
            # Sequence continues the stream monotonically (next index
            # after the frames already yielded) so the SSE ``sequence``
            # field stays contiguous.
            snapshot_end_frame = StreamFrame.end(
                frame_id=f"snap-end-{synth_seq}",
                sequence=frame_count,
                reason="snapshot",
                request_id=_snapshot_request_id,
            )
            await self._publish(
                ChatStreamFrameEvent(
                    tab_id=tab.id,
                    conversation_id=conv.id,
                    frame=snapshot_end_frame,
                ),
            )
            yield snapshot_end_frame
            frame_count += 1
        outcome.assistant_msg = assistant_msg
        outcome.frame_count = frame_count
        outcome.synth_seq = synth_seq

    async def _detect_promote_ready(self, *, conv: Any, joined: str) -> None:
        """Turn-end promote-ready detection → ``conv.detected_model`` (057).

        Extracts the model workspace path (``C:\\WoS_AI\\<model>``) from THIS
        turn's final summary ``joined`` text (the SKILL contract guarantees
        every round's summary prints that top-level path, user-visible), asks
        the injected :class:`PromoteReadyScanPort` whether that directory holds
        promote-eligible precision variants, and stashes the result on
        ``conv.detected_model`` for the caller's ``save_messages`` to persist.

        Result shape (``conv.detected_model``):
          * variants found → ``{"workdir": <path>, "variants": [{"precision",
            "label"}...], "checked_at": <iso>}``;
          * path found but NO variants → ``{"workdir": "", "variants": [],
            "checked_at": <iso>}`` (records "checked, nothing to promote" so the
            CTA hides without a re-scan);
          * no port wired / no path in the summary → left UNCHANGED (a prior
            detection from an earlier turn survives; ``None`` stays ``None``).

        Fully best-effort (AGENTS.md §5): never raises. A missing port, an
        unparseable summary, a scan error or a bad conversation are all
        swallowed so turn completion is never blocked.
        """
        port = self._promote_ready_scan
        if port is None:
            return
        try:
            root = _session_workspace_from_conv(conv) or None
            workdir = _extract_model_workdir_from_text(joined, root)
            if not workdir:
                # No model workspace path in this turn's summary — leave any
                # prior detection intact (do NOT clobber to "nothing").
                return
            variants = await port.scan(workdir)
            checked_at = self._clock.now().isoformat()
            if variants:
                conv.detected_model = {
                    "workdir": workdir,
                    "variants": [
                        {"precision": v.precision, "label": v.label}
                        for v in variants
                    ],
                    "checked_at": checked_at,
                }
            else:
                # Scanned, nothing promotable — record the empty result so the
                # CTA hides deterministically (distinct from "never checked").
                conv.detected_model = {
                    "workdir": "",
                    "variants": [],
                    "checked_at": checked_at,
                }
        except Exception:  # noqa: BLE001 — detection must never break the turn
            _log.debug(
                "chat.promote_ready.detect_failed",
                conversation_id=getattr(getattr(conv, "id", None), "value", "?"),
                exc_info=True,
            )

    async def _emit_turn_warning(
        self,
        *,
        conv: Any,
        tab: Any,
        outcome: "_TurnTailOutcome",
    ) -> AsyncIterator[StreamFrame]:
        """Emit a V1 ``turn_warning`` frame when a new threshold is crossed.

        Turn-warning slice of :meth:`_run` (B1 cohesion split).  Mirrors
        ``backend/main.py:6722-6731``: count cumulative user messages and
        emit one ``turn_warning`` frame when a new band (20 / 25 / 30 /
        ...) is crossed.  Runs BEFORE ``tab.complete_stream`` so the frame
        arrives while the tab is still ``streaming`` (the frontend
        ``applyFrame`` guard rejects frames outside ``streaming``).
        Counters advance through ``outcome``.
        """
        frame_count = outcome.frame_count
        synth_seq = outcome.synth_seq
        try:
            _user_turn_count = len(conv.messages_by_role(MessageRole.USER))
        except Exception:  # noqa: BLE001 — defensive: never fail the turn
            _user_turn_count = 0
        if _user_turn_count > 0:
            _warn_threshold = self._compute_turn_warning_threshold(
                conv.id, _user_turn_count,
            )
            if _warn_threshold > 0:
                # V1 message template (main.py:6726-6730).  Kept in sync
                # with V1 wording; the frontend also has an i18n fallback
                # (chat.turnLimitWarn) when the server omits ``message``.
                _warn_msg = (
                    f"⚠️ 当前会话已达到 {_user_turn_count} 轮对话。\n"
                    "为避免上下文过长影响回复质量，建议尽快清理历史记录或创建新会话。\n"
                    "（WebUI：点击左侧历史记录 → 新建会话；通道：发送 /new 开启新会话）"
                )
                synth_seq += 1
                turn_warning_frame = StreamFrame.turn_warning(
                    frame_id=f"turn-warn-{synth_seq}",
                    sequence=synth_seq,
                    turn_count=_user_turn_count,
                    threshold=_warn_threshold,
                    message=_warn_msg,
                )
                await self._publish(
                    ChatStreamFrameEvent(
                        tab_id=tab.id,
                        conversation_id=conv.id,
                        frame=turn_warning_frame,
                    ),
                )
                yield turn_warning_frame
                frame_count += 1
        outcome.frame_count = frame_count
        outcome.synth_seq = synth_seq

    async def _complete_turn(
        self,
        *,
        tab: Any,
        conv: Any,
        now: datetime,
        frame_count: int,
        assistant_msg: Message | None,
    ) -> None:
        """Mark the tab IDLE + publish completion (turn-tail finaliser).

        Completion slice of :meth:`_run` (B1 cohesion split).

        State-Truth-First (AGENTS.md 🔴 铁律1/3) — release the abort handle
        AT completion, atomically next to ``complete_stream``/``save``, NOT
        only in :meth:`_run`'s ``finally`` (streaming.py ``unregister`` below).

        Why this matters (queue re-send race, "排队消息重发报
        ``start_stream() requires status=IDLE, got streaming``"):
        the normal-completion ``end`` frame is yielded by
        :meth:`_finalize_assistant_message` BEFORE this method runs, and the
        WS/SSE ``done`` signal is emitted only after the generator is fully
        exhausted (i.e. after the ``finally``). The frontend dequeues the
        next queued message and re-sends it on the ``done``→idle transition.
        That re-send's :meth:`_open_stream` reads the tab and consults the
        abort registry to decide whether a STREAMING tab is *stale* (safe to
        recover to IDLE) or a *genuine* concurrent stream (must reject). If
        the handle were released only in the ``finally`` (which had not yet
        run relative to the racing re-send), the registry would still report
        ``is_streaming() is True`` while the DB tab is still STREAMING (its
        ``save`` not yet visible to the re-send's session) → the stale-tab
        self-heal (streaming.py:1929-1936) would NOT fire → ``start_stream``
        rejects. Releasing here, BEFORE ``ChatStreamCompletedEvent`` /
        ``end`` / ``done`` reach the consumer, makes ``is_streaming()`` report
        ``False`` for the just-finished turn, so the racing re-send's
        self-heal correctly recovers any residual STREAMING snapshot. The
        ``finally`` ``unregister`` stays as an idempotent backstop
        (``InMemoryStreamAbortRegistry.unregister`` is ``pop(..., None)`` —
        a double-unregister is a no-op).
        """
        tab.complete_stream(now=now)
        await self._tabs.save(tab)
        # Release the in-memory abort handle the moment the tab is durably
        # IDLE — see the docstring above (queue re-send race root-cause fix).
        self._abort_registry.unregister(tab.id)
        await self._publish(
            ChatStreamCompletedEvent(
                tab_id=tab.id,
                conversation_id=conv.id,
                completed_at=now,
                frame_count=frame_count,
            ),
        )
        _log.info(
            "chat.stream_completed",
            tab_id=tab.id.value,
            conversation_id=conv.id.value,
            frames=frame_count,
            assistant_message_id=(
                assistant_msg.id.value if assistant_msg else None
            ),
        )

    def _build_message(
        self,
        *,
        role: MessageRole,
        content: MessageContent,
        now: datetime,
        parent_id: MessageId | None,
        usage: dict[str, Any] | None = None,
        model_id: str | None = None,
        model_provider: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Message:
        # Thin wrapper over :func:`_streaming_helpers.build_message`
        # (ARCH-1 / A-3 cohesion split): forwards ``self._ids`` explicitly.
        return _build_message_fn(
            self._ids,
            role=role,
            content=content,
            now=now,
            parent_id=parent_id,
            usage=usage,
            model_id=model_id,
            model_provider=model_provider,
            meta=meta,
        )

    def _build_subagent_summary_message(
        self,
        *,
        now: datetime,
        parent_id: MessageId | None,
        sub_agent_blocks: dict[Any, dict[str, Any]],
        extra_meta: dict[str, Any] | None = None,
    ) -> Message | None:
        """Build an INDEPENDENT assistant message carrying only sub-agent fold blocks.

        SUBAGENT-RELOAD-PERSIST-INDEPENDENT-MSG (2026-07-02): the sub-agent fold
        blocks used to be MERGED into the sibling assistant message
        (tool-call-only ``tool_call_msg.meta`` or the final-summary
        ``assistant_msg.meta``). Reloading such a hybrid message rendered the
        sub-agent cards inside the same visual block as the parent agent's
        tool-call cards / summary text, whose relative order was then decided by
        the HEAD template field-order (``subAgentBlocks → toolCalls``) rather
        than by the natural conversation timeline. The frontend live path
        already put sub-agent blocks on their OWN message (via
        ``roundSubAgentMessageIds`` in ``useChatSubagent.ts``); this helper
        aligns the persisted shape with the live shape.

        The message carries the ``"[subagent_summary]"`` sentinel in
        ``content.text`` (mirroring the ``"[tool_calls]"`` sentinel — the
        domain rejects empty text) which the frontend historyMapper normalises
        back to ``""`` on load, and ``meta.kind = "subagent_summary"`` +
        ``meta.subAgentBlocks = [...]`` so a reload picks it up as an
        independent card.

        Returns ``None`` when ``sub_agent_blocks`` is empty (nothing to persist —
        caller should skip). Consumers place the result immediately AFTER the
        round message that dispatched these sub-agents (via
        :meth:`_insert_subagent_summary_messages_per_round`) so the DB row
        order matches the visible message order on reload — see the docstring
        of :meth:`_insert_subagent_summary_messages_per_round` for the
        per-round insertion contract.
        """
        if not sub_agent_blocks:
            return None
        # SUBAGENT-DISPATCH-KEY (2026-07-02, P1): iterate via the composite-
        # key-aware helper — it skips the ``"_alias_by_index"`` sentinel and
        # yields blocks in dispatch order (``(parent_round_index, index)``
        # asc). For single-dispatch turns this preserves the previous
        # ``sorted(sub_agent_blocks)`` semantics; for a multi-dispatch turn
        # it renders blocks in dispatch order rather than losing the earlier
        # dispatch to a same-``index`` overwrite.
        _blocks_list = _iter_subagent_blocks(sub_agent_blocks)
        if not _blocks_list:
            return None
        meta: dict[str, Any] = dict(extra_meta or {})
        meta["kind"] = "subagent_summary"
        meta["subAgentBlocks"] = _blocks_list
        return self._build_message(
            role=MessageRole.ASSISTANT,
            # Sentinel — domain forbids empty text (see MessageContent
            # __post_init__). The frontend historyMapper normalises this to
            # ``""`` on reload alongside the ``[tool_calls]`` sentinel.
            content=MessageContent(text="[subagent_summary]"),
            now=now,
            parent_id=parent_id,
            meta=meta,
        )

    def _insert_subagent_summary_messages_per_round(
        self,
        *,
        conv: Any,
        baseline: int,
        sub_agent_blocks: dict[Any, dict[str, Any]],
        now: datetime,
        fallback_parent_id: MessageId | None,
    ) -> None:
        """Insert one INDEPENDENT ``subagent_summary`` msg PER dispatch round.

        SUBAGENT-PER-ROUND-INSERT (2026-07-02): the previous implementation
        appended ONE subagent_summary message at the tail of the turn, right
        after the LAST tool-call round message
        (:meth:`_build_subagent_summary_message` returned a single Message
        collected for the whole turn). That was wrong on a reload for the
        common case where a turn dispatched a sub-agent in round R and then
        continued with more tool rounds (R+1, R+2, ...) — the persisted order
        became ``[round R tool cards][round R+1 tool cards]...[subagent
        panel]`` while the live stream had shown ``[round R tool cards]
        [subagent panel][round R+1 tool cards]...``. Reload diverged from
        live, breaking the "stream↔reload byte-for-byte identical" invariant
        (see :func:`_streaming_helpers.build_tool_call_message` docstring:
        "so a stream and a reload render the same message boundaries with
        ZERO inference").

        This helper walks ``conv.messages[baseline:]`` (the turn's own tail),
        groups ``sub_agent_blocks`` by each block's
        ``parent_round_index`` (recorded on SUBAGENT_START by
        :func:`_streaming_subagent_frames.accumulate_sub_agent_block`), and
        for each round R with at least one such block:

        * finds the round message whose ``meta.round_index == R`` in the
          turn tail;
        * builds ONE ``subagent_summary`` message carrying exactly the
          blocks whose ``parent_round_index == R`` (blocks sorted by their
          own ``index`` inside the message — same key
          :meth:`_build_subagent_summary_message` uses);
        * inserts the summary IMMEDIATELY AFTER that round message via
          ``conv.messages.insert(...)`` (same list-insert primitive
          :meth:`_reinsert_injected_messages` uses; skips
          ``append_message``'s parent-existence check because the
          ``parent_id`` we set is the just-found round message's id — which
          is already in the list — but we bypass that call site to avoid a
          second scan).

        Fallback path (legacy / stub / non-agentic — any block whose
        ``parent_round_index`` is absent OR whose target round message
        cannot be located in the tail): collect ALL such blocks into ONE
        subagent_summary message and APPEND it to ``conv.messages`` at the
        tail with ``parent_id = fallback_parent_id`` — byte-for-byte the
        pre-fix behaviour (the old shape a legacy consumer or an
        interrupted turn still sees). This preserves reload compat for
        older data lacking the tail-appended ``parent_round_index`` key.

        No-op when ``sub_agent_blocks`` is empty. Idempotent given the
        same inputs (the caller's rebuild path — :meth:`_persist_completed_rounds`
        — truncates ``conv.messages[baseline:]`` before rebuilding, so this
        helper always inserts into a freshly-rebuilt tail).

        SUBAGENT-DISPATCH-KEY (2026-07-02, P1): the accumulator's dict keys
        are now composite ``(parent_round_index, index)`` tuples PLUS one
        reserved string ``"_alias_by_index"`` sentinel (see
        :func:`_streaming_subagent_frames.accumulate_sub_agent_block`).
        Grouping walks only real block entries (tuple keys) and preserves
        each bucket sub-dict under its ORIGINAL composite key so
        :meth:`_build_subagent_summary_message` (via
        :func:`iter_subagent_blocks`) can serialize each bucket back in the
        same dispatch-order the accumulator produced.
        """
        if not sub_agent_blocks:
            return

        # SUBAGENT-DISPATCH-KEY: skip the ``"_alias_by_index"`` sentinel and
        # any non-tuple key defensively (composite key contract). Group
        # real blocks by ``parent_round_index``; blocks lacking the key
        # drop to the fallback bucket. Each bucket keeps its ORIGINAL
        # composite key so downstream ``iter_subagent_blocks`` sorts
        # intra-bucket by ``(parent_round, index)`` — matching the
        # single-message serializer semantics.
        by_round: dict[int, dict[tuple[int, int], dict[str, Any]]] = {}
        fallback_bucket: dict[tuple[int, int], dict[str, Any]] = {}
        for block_key, block in sub_agent_blocks.items():
            if not (isinstance(block_key, tuple) and len(block_key) == 2):
                continue
            if not isinstance(block, dict):
                continue
            pri = block.get("parent_round_index")
            if isinstance(pri, int) and not isinstance(pri, bool):
                by_round.setdefault(pri, {})[block_key] = block
            else:
                fallback_bucket[block_key] = block

        # Build an index of (round_index → position in conv.messages) over the
        # turn tail so per-round inserts locate targets in O(N) total. Bounded
        # to ``baseline..len(conv.messages)`` so a prior turn's round message
        # (should such a coincidence exist) is never mistakenly targeted.
        n = len(conv.messages)
        start = baseline if 0 <= baseline <= n else 0
        round_to_pos: dict[int, int] = {}
        for pos in range(start, n):
            meta = getattr(conv.messages[pos], "meta", None)
            if not isinstance(meta, dict):
                continue
            ri = meta.get("round_index")
            if (
                isinstance(ri, int)
                and not isinstance(ri, bool)
                and ri not in round_to_pos
            ):
                round_to_pos[ri] = pos

        # Per-round inserts, processed in descending ``round_index`` order so
        # earlier inserts do NOT shift later rounds' target positions (each
        # insert at position P shifts everything at ≥ P by +1). Rounds whose
        # target message cannot be located degrade into the fallback bucket.
        for ri in sorted(by_round.keys(), reverse=True):
            target_pos = round_to_pos.get(ri)
            if target_pos is None:
                # Block dispatched in a round that produced no persisted
                # round message this turn (e.g. a text-only round, or a
                # sub-agent dispatched from a round whose tool cards never
                # rendered a card message). Fall back to end-of-turn append.
                fallback_bucket.update(by_round[ri])
                continue
            round_msg = conv.messages[target_pos]
            summary_msg = self._build_subagent_summary_message(
                now=now,
                parent_id=round_msg.id,
                sub_agent_blocks=by_round[ri],
            )
            if summary_msg is None:  # defensive; by_round[ri] is non-empty
                continue
            conv.messages.insert(target_pos + 1, summary_msg)
            # ``updated_at`` mirrors the aggregate's own contract: the most
            # recent modification bumps it. ``append_message`` already does
            # this for tail appends; a mid-list insert must do it explicitly
            # so a later save persists the correct wall clock.
            conv.updated_at = summary_msg.created_at

        # Fallback path: append ONE catch-all subagent_summary at the tail
        # for any block that could not be placed per-round. Byte-for-byte
        # the pre-fix single-append shape a legacy reader still expects.
        if fallback_bucket:
            fallback_msg = self._build_subagent_summary_message(
                now=now,
                parent_id=fallback_parent_id,
                sub_agent_blocks=fallback_bucket,
            )
            if fallback_msg is not None:
                # Use append_message so its parent-id existence check gates
                # us against a caller passing a stale/dangling
                # ``fallback_parent_id``; the round messages we already
                # inserted are present.
                if fallback_msg.parent_id is None or any(
                    m.id == fallback_msg.parent_id for m in conv.messages
                ):
                    conv.append_message(fallback_msg)
                else:
                    # Extreme defensive fallback: caller handed a bad
                    # parent_id. Rebuild with parent_id=None so the domain
                    # invariant holds; the visual position is unchanged.
                    fallback_msg = self._build_subagent_summary_message(
                        now=now,
                        parent_id=None,
                        sub_agent_blocks=fallback_bucket,
                    )
                    if fallback_msg is not None:
                        conv.append_message(fallback_msg)

    # --- PR-401c: retry + followup helpers ---
    def _is_followup_loop_enabled(self) -> bool:
        return (
            self._max_followup_rounds > 0
            and self._guardrail_factory is not None
            and self._tool_result_truncator is not None
        )

    async def _open_initial_stream(
        self,
        *,
        conv: Any,
        tab: Any,
        request: "StreamChatInput",
        round_request_ids: dict[int, str] | None = None,
        handle: StreamAbortHandle | None = None,
    ) -> AsyncIterator[StreamFrame]:
        """Yield LLM frames, retrying on prompt-too-long / throttling.

        Async generator that combines the no-retry and retry paths.
        ``_build_llm_request`` is awaited so the App Builder Pack
        catalog (PR-091 H-4) and any future async pre-resolution
        steps complete before the LLM stream opens.

        When :class:`RetryPolicyPort` is wired the generator delegates
        to :meth:`_open_with_retry`, which inspects the first frame
        for retryable error codes; otherwise it just forwards the raw
        ``llm.stream(...)`` iterator one frame at a time.

        ``round_request_ids`` (when supplied) receives this round's
        prompt-snapshot id at key ``0`` — the initial LLM call is agentic
        round 0 (V1 parity: the first ``/api/chat`` request).  Saved AFTER
        ``_build_llm_request`` so ``request.extra["system_prompt"]`` is the
        fully-built prompt actually sent this round.
        """
        if self._retry_policy is None:
            # P0-2: use presend-compressed history if available.
            _presend = getattr(self, "_presend_compressed", None)
            self._presend_compressed = None  # type: ignore[attr-defined]
            llm_req = await self._build_llm_request(
                conv=conv, tab=tab, request=request,
                compressed_history=_presend,
            )
            await self._capture_round_zero_snapshot(
                conv=conv, request=request, llm_req=llm_req,
                round_request_ids=round_request_ids,
            )
            async for frame in self._llm.stream(llm_req):
                yield frame
            return
        async for frame in self._open_with_retry(
            conv=conv, tab=tab, request=request,
            round_request_ids=round_request_ids,
            handle=handle,
        ):
            yield frame

    async def _open_with_retry(
        self,
        *,
        conv: Any,
        tab: Any,
        request: "StreamChatInput",
        round_request_ids: dict[int, str] | None = None,
        handle: StreamAbortHandle | None = None,
    ) -> AsyncIterator[StreamFrame]:
        """Generator that yields LLM frames after applying retry policy.

        Inspects the **first** frame returned by the LLM; when it is
        an ERROR with a known retryable category, consults
        :class:`RetryPolicyPort`.  If a retry is approved, sleeps the
        prescribed delay (asyncio.sleep, mockable via the ``sleep`` ctor
        kwarg) and re-opens the stream.  When the retry budget is
        exhausted the original error frame is forwarded to the caller.

        When :class:`ContextCompressionPort` is wired and a
        ``PROMPT_TOO_LONG`` retry is approved, the compressor is invoked
        on the conversation history before re-opening the stream.

        ``NETWORK`` errors (transient connect / timeout / read / socket
        failures) retry **indefinitely** with an escalating capped backoff
        (3s → 5s → 10s → 30s → 30s …) until the link recovers or the user
        aborts. Because the first-frame inspection is the seam, this only
        covers a connection that fails BEFORE any content streams (which is
        exactly when the adapter emits its ``chat.llm.connect_error`` /
        ``timeout`` frame) — so a retry never replays already-streamed text.
        The backoff sleep is abortable via ``handle`` so a user Stop ends the
        wait at once. The non-network ``max_iterations`` ceiling does NOT
        bound network attempts.
        """
        attempt_throttling = 0
        attempt_prompt = 0
        attempt_network = 0
        attempt_bounded_fast = 0
        attempt_bounded_server = 0
        # Bounds the PROMPT_TOO_LONG / THROTTLING / BOUNDED_FAST /
        # BOUNDED_SERVER retries; we reach a terminal decision long before
        # this ceiling unless the policy is mis-configured. Only NETWORK is
        # intentionally exempt from this attempt ceiling (it is instead
        # bounded by a wall-clock budget — see ``network_started_ms`` below),
        # so NETWORK does NOT consume ``non_network_iterations``.
        max_iterations = 16
        non_network_iterations = 0
        # Wall-clock budget anchor for NETWORK retries: monotonic ms of the
        # FIRST network retry this turn. ``elapsed_s`` (now - anchor) is passed
        # to the policy so a never-recovering network terminates once the
        # cumulative retry time exceeds the policy's budget (was: infinite).
        network_started_ms: int | None = None
        # Compressed history override; None means "use conv.messages as-is".
        # P0-2: if a presend-compress ran, use its result as the initial
        # compressed history so the first LLM call already benefits.
        compressed_history: tuple[Any, ...] | None = getattr(
            self, "_presend_compressed", None
        )
        # Clear the per-turn stash so it doesn't leak to the next turn.
        self._presend_compressed: tuple[Any, ...] | None = None  # type: ignore[attr-defined]

        while True:
            # Honour a user Stop that landed between retry attempts BEFORE
            # opening another stream (State-Truth-First: don't burn an attempt
            # the user already cancelled).
            if handle is not None and handle.is_set():
                return
            if non_network_iterations >= max_iterations:
                # Safety ceiling for the bounded categories only (never hit on
                # a well-configured policy); network retries bypass it.
                return
            llm_req = await self._build_llm_request(
                conv=conv,
                tab=tab,
                request=request,
                compressed_history=compressed_history,
            )
            stream = self._llm.stream(llm_req)
            stream_iter = stream.__aiter__()
            try:
                first_frame = await stream_iter.__anext__()
            except StopAsyncIteration:
                # Empty stream — nothing to yield, return.
                return

            category = self._classify_error_frame(first_frame)
            if category is None:
                # Normal first frame; yield it and drain the rest. The build
                # that produced a non-error first frame is the one actually
                # answering this turn — capture ITS round-0 snapshot (after
                # any prompt-too-long compression retries settled).
                await self._capture_round_zero_snapshot(
                    conv=conv, request=request, llm_req=llm_req,
                    round_request_ids=round_request_ids,
                )
                yield first_frame
                async for f in stream_iter:
                    yield f
                return

            # Retry path.
            assert self._retry_policy is not None  # mypy
            if category is RetryCategory.PROMPT_TOO_LONG:
                non_network_iterations += 1
                attempt_prompt += 1
                decision = self._retry_policy.next_attempt(
                    category=RetryCategory.PROMPT_TOO_LONG,
                    attempt_number=attempt_prompt,
                )
            elif category is RetryCategory.NETWORK:
                # Budget-capped (was unbounded): a network fault retries with
                # escalating backoff until it recovers OR the cumulative retry
                # wall-clock exceeds the policy budget. Does NOT consume
                # ``non_network_iterations`` (its bound is time, not attempts).
                attempt_network += 1
                if network_started_ms is None:
                    network_started_ms = _now_ms(self._clock)
                    elapsed_s = 0.0
                else:
                    elapsed_s = max(
                        0.0,
                        (_now_ms(self._clock) - network_started_ms) / 1000.0,
                    )
                decision = self._retry_policy.next_attempt(
                    category=RetryCategory.NETWORK,
                    attempt_number=attempt_network,
                    elapsed_s=elapsed_s,
                )
            elif category is RetryCategory.BOUNDED_FAST:
                # DNS / connection-refused / host-unreachable: a few fast
                # bounded attempts. These DO count against the ceiling (unlike
                # NETWORK) so a mis-configured policy can never loop forever.
                non_network_iterations += 1
                attempt_bounded_fast += 1
                decision = self._retry_policy.next_attempt(
                    category=RetryCategory.BOUNDED_FAST,
                    attempt_number=attempt_bounded_fast,
                )
            elif category is RetryCategory.BOUNDED_SERVER:
                # HTTP 5xx: a few jittered bounded attempts; counts against the
                # ceiling.
                non_network_iterations += 1
                attempt_bounded_server += 1
                decision = self._retry_policy.next_attempt(
                    category=RetryCategory.BOUNDED_SERVER,
                    attempt_number=attempt_bounded_server,
                )
            else:  # THROTTLING
                non_network_iterations += 1
                attempt_throttling += 1
                # Honor a server-advised ``Retry-After`` when the upstream
                # provided one (the LLM adapter parses it into the error
                # frame's ``retry_after_seconds`` payload — absent → None →
                # the policy falls back to its exponential+jitter schedule).
                decision = self._retry_policy.next_attempt(
                    category=RetryCategory.THROTTLING,
                    attempt_number=attempt_throttling,
                    server_advised_delay_s=first_frame.payload.get(
                        "retry_after_seconds"
                    ),
                )

            if not decision.should_retry:
                # Forward the (last) error frame; let the caller surface it.
                yield first_frame
                async for f in stream_iter:
                    yield f
                return

            # Log the actual error detail before retrying so the cause is
            # diagnosable from the log (previously only the retry attempt
            # number was logged, making SSL / connect failures invisible).
            _log.warning(
                "chat.stream_retry.error_detail",
                attempt=decision.attempt_number,
                category=category.value,
                code=first_frame.payload.get("code"),
                message=first_frame.payload.get("message"),
            )
            _log.info(
                "chat.stream_retry",
                category=category.value,
                attempt=decision.attempt_number,
                delay_seconds=decision.delay_seconds,
                compress_target_ratio=decision.compress_target_ratio,
            )

            # Context compaction on prompt-too-long retry.
            #
            # ROOT FIX: route through the session compaction checkpoint
            # (含-tool-output WIRE) instead of the old ``_compress_history``
            # (content.text-only, dropped tool output → barely shrank a
            # tool-heavy prompt the provider just rejected). ``force=True``
            # because the provider ALREADY rejected the prompt, so compress
            # regardless of the local threshold estimate. On success the
            # checkpoint advances and we clear ``compressed_history`` so the
            # re-opened ``_build_llm_request`` assembles the compacted wire
            # from the checkpoint (compacted head + increment). ``conv.messages``
            # stays the full original history.
            if (
                category is RetryCategory.PROMPT_TOO_LONG
                and self._context_compressor is not None
                and decision.compress_target_ratio is not None
            ):
                changed = await self._compress_via_checkpoint(
                    conv=conv,
                    request=request,
                    target_ratio=decision.compress_target_ratio,
                    force=True,
                )
                if changed:
                    compressed_history = None

            if decision.delay_seconds > 0:
                if category in (
                    RetryCategory.NETWORK,
                    RetryCategory.BOUNDED_FAST,
                    RetryCategory.BOUNDED_SERVER,
                ):
                    # Surface a transient "retrying" progress frame so the UI
                    # shows the network-retry banner (otherwise round 0 is
                    # silent for up to 30s per attempt). Non-terminal — a
                    # successful retry then streams normal frames. All three
                    # connectivity categories (transient NETWORK + the bounded
                    # DNS/refused/unreachable + 5xx) share the banner and the
                    # abortable backoff so a user Stop ends any wait at once.
                    yield self._network_retry_frame(
                        attempt=decision.attempt_number,
                        delay_seconds=decision.delay_seconds,
                        code=first_frame.payload.get("code"),
                    )
                    # Backoff can be long (up to 30s) so the wait MUST be
                    # abortable — a user Stop ends it at once. Slicing the
                    # sleep changes the observable ``_sleep`` cadence, which is
                    # why the THROTTLING / PROMPT_TOO_LONG branches below keep
                    # the original single-shot ``_sleep`` call.
                    aborted = await self._abortable_sleep(
                        decision.delay_seconds, handle
                    )
                    if aborted:
                        # User pressed Stop during the backoff wait. Stop
                        # retrying; the caller's abort tail releases the tab
                        # (the user chose to give up — do NOT forward the
                        # error frame).
                        return
                else:
                    # PROMPT_TOO_LONG / THROTTLING: short, bounded waits — keep
                    # the original single ``_sleep`` call (behaviour unchanged).
                    await self._sleep(decision.delay_seconds)
            # Loop continues — re-open stream.

    def _classify_error_frame(
        self,
        frame: StreamFrame,
    ) -> RetryCategory | None:
        """Map an ERROR frame's ``code`` to a :class:`RetryCategory`.

        Thin wrapper over :func:`_streaming_helpers.classify_error_frame`
        (ARCH-1 / A-3 cohesion split): the function never used ``self``;
        logic byte-for-byte unchanged.
        """
        return _classify_error_frame_fn(frame)

    # ------------------------------------------------------------------
    # Real-prompt-token attribution (real-token differential compression)
    # ------------------------------------------------------------------

    @staticmethod
    def _real_prompt_for_initial_round(
        *,
        usage: dict[str, Any] | None,
        wire: list[dict[str, Any]],
        model_hint: str | None,
    ) -> tuple[int, int, str]:
        """Real prompt-token size for round 0 + completion + source label.

        Round 0's input wire = ``system + tools + user`` (the base wire
        assembled at the start of the agentic loop). Provider-measured
        ``initial_usage`` reports its true size as ``prompt_tokens`` (already
        ``_extract_usage``-corrected for cache-hit turns:
        ``llm_stream.py:1917-1921``). When no usage is available (local
        models / empty usage block), fall back to a SINGLE bounded tokenizer
        pass over the wire's text content — bounded because the wire at
        round 0 is just system + tools + the FRESH user message, no history
        tool replies yet, so the cost is small and one-shot (NOT the
        per-history full re-tokenisation forbidden as the project performance
        red line — see ``CONTEXT-COMPRESSION.md`` §1).

        Returns ``(real_prompt_tokens, completion_tokens, source)`` where
        ``source`` is ``"cloud"`` / ``"tokenizer"`` / ``"unknown"`` — the
        compaction differential path treats ``"unknown"`` (0) as a fallback
        signal and uses char × density for that group.
        """
        if isinstance(usage, dict):
            try:
                pt = int(usage.get("prompt_tokens") or 0)
                ct = int(usage.get("completion_tokens") or 0)
            except (TypeError, ValueError):
                pt = 0
                ct = 0
            if pt > 0:
                return pt, ct, "cloud"
        # Tokenizer fallback: one pass over wire text content. Bounded —
        # round 0 wire is system + tools + user, no history tool outputs yet.
        try:
            text_blob = "\n".join(
                str(m.get("content") or "") for m in wire
            )
            est = _precise_text_tokens(text_blob, model_hint)
            if est is not None:
                return int(est), 0, "tokenizer"
        except Exception:  # noqa: BLE001 — token attribution never breaks chat
            _log.warning(
                "chat.real_prompt.tokenizer_fallback_failed",
                round_no=0,
                model_hint=model_hint,
            )
        return 0, 0, "unknown"

    @staticmethod
    def _estimate_vision_tokens(items: Any) -> int:
        """Estimate prompt-token cost of any OpenAI-shape vision blocks found.

        CCD-6 (PENDING-WORK.md §1): the tokenizer fallback in
        :meth:`_extract_round_real_prompt` only measures **text** parts of new
        tool results / assistant lead-in; for a hypothetical future local
        vision chat model running offline (no cloud usage block), the image
        content would be silently dropped from the real-prompt counter and
        the badge would under-count, breaking the compaction trigger.

        Strategy: walk ``items`` recursively and count every dict that looks
        like an OpenAI vision content block:

        * ``{"type": "image_url", "image_url": {"url": ..., "detail"?,
           "width"?, "height"?}}`` — OpenAI canonical;
        * ``{"type": "image", ...}``  — Anthropic-shape variant
          (data + media_type); estimated by the same rule.

        Per-image cost follows OpenAI's published formula:
        ``base = 85`` (low-detail / unknown size) plus ``170`` per 512x512
        tile of the image. When the block carries explicit ``width``/
        ``height`` we compute exact ``tiles = ceil(w/512) * ceil(h/512)``.
        Otherwise we conservatively assume a 1024x1024 image, i.e.
        ``tiles=4`` → 85 + 4*170 = 765 tokens per image. Detail="low" caps
        at the base 85 tokens.

        Best-effort and never raises: this is a safety-net for an unmeasured
        local model; cloud paths use the provider's exact ``usage`` block
        instead, which already accounts for vision tokens.
        """
        if not items:
            return 0

        def _tile_cost(width: Any, height: Any, detail: Any) -> int:
            # "low" detail mode is a fixed 85 tokens regardless of size.
            if isinstance(detail, str) and detail.lower() == "low":
                return 85
            try:
                w = int(width) if width is not None else None
                h = int(height) if height is not None else None
            except (TypeError, ValueError):
                w = h = None
            if w and h and w > 0 and h > 0:
                tiles = math.ceil(w / 512) * math.ceil(h / 512)
            else:
                # Unknown dimensions → conservative 1024x1024 (4 tiles).
                tiles = 4
            return 85 + tiles * 170

        total = 0

        def _walk(obj: Any) -> None:
            nonlocal total
            if isinstance(obj, dict):
                t = obj.get("type")
                if t == "image_url":
                    img = obj.get("image_url")
                    if isinstance(img, dict):
                        total += _tile_cost(
                            img.get("width"), img.get("height"), img.get("detail"),
                        )
                    else:
                        # Bare ``image_url`` value (string URL): treat as one
                        # unknown-size image.
                        total += _tile_cost(None, None, None)
                    return
                if t == "image":
                    # Anthropic shape: ``{"type":"image","source":{...},
                    # "width"?, "height"?, "detail"?}``.
                    total += _tile_cost(
                        obj.get("width"), obj.get("height"), obj.get("detail"),
                    )
                    return
                for v in obj.values():
                    _walk(v)
            elif isinstance(obj, list | tuple):
                for v in obj:
                    _walk(v)

        try:
            _walk(items)
        except Exception:  # noqa: BLE001 — vision estimate never breaks chat
            return 0
        return total

    @staticmethod
    def _extract_round_real_prompt(
        *,
        usage: dict[str, Any] | None,
        new_tool_results: list[dict[str, Any]],
        assistant_text: str,
        model_hint: str | None,
        prev_real_prompt: int = 0,
    ) -> tuple[int, int, str]:
        """Cumulative real prompt-tokens for a follow-up round + completion + src.

        For an agentic-loop follow-up round (round_no ≥ 1), the round's
        ``real_prompt_tokens`` is the size of the wire IT RECEIVED as input
        — i.e. the wire BEFORE this round added its own atomic group block
        (assistant{tool_calls} + tool replies). The provider's
        ``end_payload.usage.prompt_tokens`` from THIS round's stream is
        exactly that — already ``_extract_usage``-corrected against
        cache-hit under-reporting.

        **Invariant**: ``real_prompt_tokens`` is **always cumulative** in the
        returned tuple regardless of source. Cloud path returns
        ``usage.prompt_tokens`` directly. Tokenizer-fallback path measures
        only THIS round's incremental content (new tool result bodies +
        assistant lead-in — NOT the whole wire, the project performance red
        line) and adds it to ``prev_real_prompt`` (the previous round's
        cumulative) so the differential compressor can still compute
        ``seq[end] - seq[start]`` uniformly across mixed sources. Returns 0 +
        ``"unknown"`` when tokenizer is unavailable; the compressor falls
        back to char × density for that one group (per-group, not whole
        wire) — see ``CONTEXT-COMPRESSION.md`` §3.

        Returns ``(real_prompt_tokens, completion_tokens, source)``.
        """
        if isinstance(usage, dict):
            try:
                pt = int(usage.get("prompt_tokens") or 0)
                ct = int(usage.get("completion_tokens") or 0)
                tt = int(usage.get("total_tokens") or 0)
            except (TypeError, ValueError):
                pt = 0
                ct = 0
                tt = 0
            # Belt-and-braces: ``_extract_usage`` already does this max
            # correction, but recomputing here costs nothing and protects
            # against alternate usage shapes that may slip past the
            # extractor on third-party providers.
            if tt > 0 and ct >= 0:
                derived = tt - ct
                if derived > pt:
                    pt = derived
            if pt > 0:
                return pt, ct, "cloud"
        # Tokenizer fallback: only the increment (new tool result bodies +
        # the assistant lead-in text), then add ``prev_real_prompt`` so the
        # stored value stays cumulative (monotonic across rounds). NEVER
        # re-tokenises the full wire. Returns 0 / "unknown" on failure →
        # graceful per-group char × density fallback in Level 3.
        try:
            parts: list[str] = []
            if assistant_text:
                parts.append(assistant_text)
            for tr in new_tool_results:
                out = tr.get("output") if isinstance(tr, dict) else None
                if isinstance(out, str):
                    parts.append(out)
                elif out is not None:
                    parts.append(json.dumps(out, ensure_ascii=False))
            blob = "\n".join(parts)
            est = _precise_text_tokens(blob, model_hint)
            if est is not None:
                # CCD-6: add a vision-block estimate so a hypothetical future
                # local vision chat model running offline (no cloud usage)
                # does not silently drop image tokens from the running real
                # prompt counter (which would under-count the wire and miss
                # the compaction trigger). Cloud paths take the ``usage``
                # branch above and never reach here, so this is a pure
                # safety-net for the tokenizer fallback. ``output`` is the
                # only field that can carry vision blocks in a tool reply
                # (assistant text is plain string here).
                image_est = StreamChatUseCase._estimate_vision_tokens(
                    [tr.get("output") for tr in new_tool_results
                     if isinstance(tr, dict)]
                )
                return (
                    int(est) + image_est + max(0, prev_real_prompt),
                    0,
                    "tokenizer",
                )
        except Exception:  # noqa: BLE001 — token attribution never breaks chat
            _log.warning(
                "chat.real_prompt.tokenizer_fallback_failed",
                model_hint=model_hint,
            )
        return 0, 0, "unknown"

    @staticmethod
    def _accumulate_usage(
        accumulator: dict[str, int], usage: dict[str, Any] | None
    ) -> None:
        """Sum a round's token ``usage`` into ``accumulator`` in place.

        V1 parity (useChat.js:2351-2356/2411): the per-round token usage is
        **accumulated** across the agentic loop — the final figure the user
        sees is the sum over every LLM round, not just the last round.  V2
        moves this aggregation server-side (architecture: the backend owns
        the canonical turn usage; the END frame carries the running total so
        the front-end need not re-implement summation and a reload re-shows
        the correct number).

        Only integer-valued keys are summed (``prompt_tokens`` /
        ``completion_tokens`` / ``total_tokens`` / ``cache_read_tokens``).
        Non-integer / unknown keys are ignored.
        """
        if not isinstance(usage, dict):
            return
        for key, value in usage.items():
            if isinstance(value, bool):
                # ``is_mock`` etc. — never sum booleans.
                continue
            if isinstance(value, int):
                accumulator[key] = accumulator.get(key, 0) + value

    @staticmethod
    def _append_display_usage_fields(
        usage: dict[str, Any],
        last_round_usage: dict[str, Any] | None,
        first_round_usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Tail-append the keystone last-round + DISPLAY-ONLY cache figures.

        SINGLE source of truth for the ``last_round_*`` tail keys that the
        token badge reads (↑new = input − cache_read − cache_write).
        BOTH the persisted-message path (:meth:`_finalize_assistant_message`)
        AND the live END-frame path (``_drain_followup_rounds`` terminal END)
        call this so the two口径 can NEVER drift — the drift is exactly the
        "live ↑ shows the whole prompt (缓存未扣), reload ↑ shows the adjusted
        value" bug (live END frame used to omit these fields → the front-end
        ``last_round_cache_read_display`` fell back to 0 → Σ full prompt).

        口径 (byte-identical to the former inline block at the persist path):
          * ``last_round_prompt_tokens``       = last_round.prompt_tokens
          * ``last_round_cache_read_tokens``   = last_round.cache_read_tokens
            (the eff-prompt keystone — DELIBERATELY the possibly-ZEROED value;
            counter/billing math reads THIS, never the display field below)
          * ``last_round_cache_read_display``  = cache_read_observed
                                                 ?? cache_read_tokens ?? 0
          * ``last_round_cache_write_display`` = cache_write_observed ?? 0
          * ``first_round_prompt_tokens``      = (first_round ?? last_round)
                                                 .prompt_tokens

        AGENTS.md §3.1: tail-only append (SHAPE unchanged, only adds keys). The
        display fields are DISPLAY-ONLY — counter/eff_prompt math never reads
        them, and ``cache_read_tokens`` stays whatever ``_extract_usage`` set
        (zeroed on a cache-hit turn to protect billing double-add). No-op when
        ``last_round_usage`` is absent (returns ``usage`` unchanged) so legacy /
        no-usage turns keep the prior shape.

        DRY (2026-07-03): the actual口径 now lives in the SHARED module-level
        :func:`qai.chat.application._token_estimate_helpers.append_display_usage_fields`
        so the sub-agent per-round usage stamp (``agent_tool``) uses the exact
        same rule — this thin wrapper only preserves the historic name/call
        sites (byte-for-byte identical output; the shared function is a 1:1 copy
        of the former inline block).
        """
        return _append_display_usage_fields_shared(
            usage, last_round_usage, first_round_usage
        )

    @staticmethod
    def _finalize_turn_usage(
        accumulator: dict[str, int],
        last_round_usage: dict[str, Any] | None,
        model_hint: str | None,
    ) -> dict[str, int]:
        """Return the END-frame ``usage`` with ``prompt_tokens`` corrected.

        ``_accumulate_usage`` SUMs every integer key round-over-round. That is
        correct for ``completion_tokens`` (each round's output is independent and
        additive) and for ``cache_read_tokens``, but **wrong** for
        ``prompt_tokens`` / ``total_tokens`` on cumulative-prompt providers
        (Anthropic / Claude): each round RE-SENDS the full conversation + all
        prior tool results, so a round's ``prompt_tokens`` is ALREADY the running
        wire size. Summing it across rounds is quadratic — round 0 (~50K) +
        round 1 (~80K) + ... balloons to millions by round 17 (the observed
        ``turn_usage_prompt_tokens=10552258`` bug). The persisted assistant
        message + END frame then showed that bogus 10M figure.

        Fix (focused on the END-frame assembly, NOT on the generic
        ``_accumulate_usage`` — so ``completion_tokens`` / ``cache_read_tokens``
        keep their correct SUM): for anthropic-family models override
        ``prompt_tokens`` with the LAST round's value (the true final wire size),
        and recompute ``total_tokens = prompt_tokens(last round) +
        completion_tokens(SUM)`` so the dict stays self-consistent. Every other
        key is preserved verbatim from the accumulator.

        Invariants (kept zero-behaviour-change where the SUM was already right):
        * **Single-round turn**: last_round == accumulated, so the override is a
          no-op numerically (prompt unchanged, total = prompt + completion which
          for a single round already equals the model's total).
        * **Non-anthropic provider**: returns ``dict(accumulator)`` untouched —
          their per-round ``prompt_tokens`` is the current round only, so the SUM
          口径 is left exactly as before.
        * **No last-round usage captured**: falls back to the accumulator as-is
          (cannot correct without the keystone figure).

        AGENTS.md §3.1: the END-frame ``usage`` SHAPE (field names / types) is
        unchanged. Only the runtime VALUE of ``prompt_tokens`` / ``total_tokens``
        is corrected from the bogus SUM to the true last-round wire size — this
        fixes an error value, it does not alter the contract.

        C档 阶段2: the correction口径 is now the shared domain pure function
        :func:`qai.chat.domain.usage_math.finalize_cumulative_prompt_usage`
        (same rule the sub-agent path uses), so the "anthropic prompt = last
        round, total recomputed" logic lives in ONE place. This method keeps its
        signature + behaviour; it just delegates the math.
        """
        return finalize_cumulative_prompt_usage(
            accumulator,
            last_round_usage,
            is_cumulative=_is_anthropic_family(model_hint),
        )

    @staticmethod
    def _resolved_user_turn(
        request: "StreamChatInput",
    ) -> dict[str, Any]:
        """Return the single trailing ``role: user`` dict for this turn's wire.

        P11 helper — single source of truth for "what does the current
        user's message look like on the wire?". Honours
        ``request.extra["image_content_blocks"]`` (the pre-resolved
        OpenAI vision blocks the route layer assembled from
        ``media_refs``); falls back to plain ``request.user_message.text``
        otherwise. Used by :meth:`_build_base_wire_messages` for BOTH
        take-over and non-take-over base wires, and (after P11) by the
        ``_run`` entry's take-over pre-build so round 0 lands on the
        same multimodal shape round 1+ would produce — closing the
        report A.2 divergence (multimodal take-over compact_hook
        previously dropped images, P12).
        """
        extra = request.extra if isinstance(request.extra, dict) else {}
        vision_blocks = extra.get("image_content_blocks") if extra else None
        if isinstance(vision_blocks, list) and vision_blocks:
            return {"role": "user", "content": list(vision_blocks)}
        prompt_text = getattr(request.user_message, "text", "") or ""
        return {"role": "user", "content": prompt_text}

    def _build_base_wire_messages(
        self,
        *,
        conv: Any,
        request: "StreamChatInput",
        compressed_history: tuple[Any, ...] | None,
    ) -> list[dict[str, Any]]:
        """Build the base wire history: replayed conversation + current user.

        V1 parity (chat_handler.py:436 ``_build_system_messages``): the
        agentic loop starts from ``system + full history + current user``
        and then **grows one list** (``full_messages``), appending an
        ``assistant{content, tool_calls}`` + paired ``role:tool`` block per
        round (chat_handler.py:791-859).  This returns the base (history +
        current user); :meth:`_append_tool_round` appends each round's
        block, mirroring V1's incremental ``full_messages.append(...)``.

        The system message is NOT added here — the adapter prepends it from
        ``extra["system_prompt"]`` (llm_stream.py:816-819).

        User take-over (``extra["_subagent_takeover_wire"]``): when set, the
        base wire is the sub-agent's persisted history (system turn already
        dropped) instead of the parent conversation, so the user continues the
        sub-agent's own thread. The current user prompt is still appended below.

        P11 — single source of truth for take-over wire construction.
        Previously the take-over base wire was built in TWO places: here
        (used for follow-up rounds + ``compact_hook`` re-base) AND in
        :meth:`_build_llm_request` (used for round 0). The two
        implementations had subtle divergence in the user-turn
        construction (this one ALWAYS used plain text, the other used
        the multimodal user block when present), and the duplication
        required a ``_is_followup_round`` guard to avoid running both on
        the same turn. Plan X collapses both to this single function;
        multimodal awareness is unified here via :meth:`_resolved_user_turn`
        so a take-over compact_hook re-base no longer drops images.
        """
        takeover_wire = None
        _extra = request.extra if isinstance(request.extra, dict) else None
        if _extra is not None:
            tw = _extra.get("_subagent_takeover_wire")
            if isinstance(tw, list):
                takeover_wire = tw
        if takeover_wire is not None:
            messages: list[dict[str, Any]] = [dict(m) for m in takeover_wire]
            messages.append(self._resolved_user_turn(request))
            # Repair any orphan ``role=tool`` rows so the downstream pre-send
            # sanitiser does not DELETE them every round (which would strand
            # the model with a structurally-broken, ever-shrinking context and
            # drive the reported "rebuild the same plan / repeat the same
            # sentence" infinite tool loop). Folding (not dropping) keeps the
            # information AND keeps the wire valid so no row is removed on each
            # round. Clean wires pass through unchanged.
            return _repair_orphan_tool_messages(messages)

        if compressed_history is not None:
            # Explicit override (synthetic-retry path) — used as-is, bypassing
            # the session compaction checkpoint (the synthetic nudge already
            # carries the history it wants the next round to send).
            history = compressed_history
            messages = _rebuild_history_wire_messages(history)
        else:
            # No explicit override: consult the session compaction checkpoint
            # (dual-track history). When present, the base wire is the
            # compacted head + the verbatim increment past the anchor (the
            # SAME含-tool-output wire used for estimation/compression); when
            # absent, rebuild the full live history. ``conv.messages`` is never
            # mutated by compaction.
            ckpt = self._compaction_checkpoints.get(self._conv_key(conv))
            if ckpt is not None:
                messages = self._assemble_history_wire(conv=conv, request=request)
            else:
                history = _drop_trailing_current_user(
                    tuple(conv.messages), current=request.user_message
                )
                # User-explicit hard round cap (``/compact <N>``), applied
                # BEFORE token compaction and only on this no-checkpoint live
                # history (when a checkpoint exists we trust the token
                # compaction result and do NOT also round-trim — keeping the
                # dual-track anchor indices intact). Trims on a round boundary
                # so no tool round is sliced apart (no orphan ``role=tool``);
                # the remaining history is still handed to normal token
                # compaction downstream. V1 parity:
                # ``wechat/channel.py:237`` ``_trim_history_by_rounds``.
                _rounds = self._max_history_rounds_override(request)
                if _rounds is not None:
                    _before = len(history)
                    history = trim_messages_by_rounds(history, _rounds)
                    if len(history) != _before:
                        _log.info(
                            "chat.history.round_trimmed",
                            conv_key=self._conv_key(conv),
                            max_history_rounds=_rounds,
                            messages_before=_before,
                            messages_after=len(history),
                        )
                # Rebuild prior tool-using rounds as proper
                # assistant{tool_calls} + role:tool blocks (V1 parity
                # chat_handler.py:_build_system_messages): a flat {role,
                # content} replay dropped every historical tool linkage and
                # leaked the internal "[tool_calls]" sentinel to the model on a
                # later turn.  ``rebuild_history_wire_messages`` restores the
                # wire shape; the adapter's sanitiser drops any orphaned
                # pairing.
                messages = _rebuild_history_wire_messages(history)

        # Current user prompt (mirrors ``_build_llm_request``'s single-list
        # semantics: ``history`` excludes the trailing current-user turn,
        # which is re-added here explicitly). P11: routed through
        # :meth:`_resolved_user_turn` so a multimodal turn re-runs through
        # this builder (compact_hook / followup-round re-base) keeps its
        # vision blocks instead of degrading to plain text — the same
        # multimodal user shape ``_build_llm_request`` previously
        # constructed inline.
        messages.append(self._resolved_user_turn(request))
        # REPAIR (not drop) orphan ``role=tool`` rows here too — this is the
        # base wire the multi-round followup loop GROWS each round and re-feeds
        # to the LLM. If the rebuilt history strands an orphan tool (e.g. an
        # interrupted prior round, or an assistant turn whose tool_calls were
        # never recorded), the pre-send sanitiser would delete it on EVERY
        # round → the model never sees a coherent tool result → it loops
        # re-deciding (the reported infinite loop). Folding the orphan into
        # adjacent assistant text keeps the wire valid so nothing is dropped
        # round after round. Clean histories are byte-for-byte unaffected.
        return _repair_orphan_tool_messages(messages)

    @staticmethod
    def _append_tool_round(
        wire_messages: list[dict[str, Any]],
        *,
        assistant_text: str,
        round_tool_calls: list[StreamFrame],
        round_results: list[dict[str, Any]],
    ) -> None:
        """Append one round's V1-style ``assistant{tool_calls}`` + tools.

        Faithful to V1 chat_handler.py:775-859 — the assistant message that
        issued a round's tool calls carries that round's lead-in ``content``
        and **only that round's** ``tool_calls``; it is immediately followed
        by one ``role:tool`` message per executed call, matched by
        ``tool_call_id``.  The list grows round by round (V1's
        ``full_messages.append(...)``) instead of flattening every call into
        a single assistant message — so the model sees the real
        call → result → next-thought → call causal chain.

        ``round_tool_calls`` are the TOOL_CALL frames the model emitted for
        this round; ``round_results`` are the matching executed-result dicts
        (same order / count).  ``tool_call_id`` is taken from the result dict
        (falling back to the frame payload, then a synthesised id) so the
        ``assistant.tool_calls[i].id`` and the ``role:tool.tool_call_id``
        always agree.
        """
        if not round_results:
            return

        # Build ``(name, args, call_id)`` metas + the per-call result strings
        # and thought_signatures, resolving each call_id with the main loop's
        # precedence (result dict id → originating frame payload id → a
        # round-unique synthetic id; never a bare ``call_{i}`` that would
        # collide across rounds when the upstream omits ids).  The actual
        # OpenAI ``assistant.tool_calls`` block + paired ``role:tool`` replies
        # are then rendered by the SHARED neutral kernel builders — the SAME
        # ones the sub-agent loop uses — so both loops emit byte-identical wire
        # shape (strict id pairing, ``ensure_ascii=False``).
        tool_metas: list[tuple[str, dict[str, Any], str]] = []
        result_texts: list[str] = []
        thought_signatures: dict[str, Any] = {}
        for i, tr in enumerate(round_results):
            call_id = tr.get("tool_call_id")
            if not call_id and i < len(round_tool_calls):
                call_id = round_tool_calls[i].payload.get("tool_call_id")
            if not call_id and i < len(round_tool_calls):
                call_id = f"fu_{round_tool_calls[i].frame_id}"
            if not call_id:
                call_id = f"call_{i}"
            name = tr.get("tool_name") or "tool"
            args = tr.get("arguments")
            tool_metas.append(
                (name, args if isinstance(args, dict) else {}, call_id)
            )
            result_texts.append(str(tr.get("result", "")))
            # PR-090 C-1: re-attach the Vertex AI thought_signature verbatim
            # so the next turn's request echoes it back (V1
            # chat_handler.py:787-789); absent for non-Vertex providers.
            sig = tr.get("thought_signature")
            if sig:
                thought_signatures[call_id] = sig

        tool_calls_block = _kernel_assistant_tool_calls_block(
            tool_metas, thought_signatures=thought_signatures
        )
        tool_reply_msgs = _kernel_tool_reply_blocks(tool_metas, result_texts)
        # V1 chat_handler.py:791-795 — ``content`` is the round's lead-in
        # text (``_followup_text or None``); pass the actual text so the
        # model retains its own per-round reasoning, not a blanked-out None.
        wire_messages.append(
            {
                "role": "assistant",
                "content": (assistant_text or None),
                "tool_calls": tool_calls_block,
            }
        )
        wire_messages.extend(tool_reply_msgs)

    def _maybe_inject_question_images(
        self,
        wire_messages: list[dict[str, Any]],
        round_results: list[dict[str, Any]],
    ) -> None:
        """Inject a follow-up multimodal ``role:user`` message for question
        images (V2 enhancement).

        A ``question`` tool answer may embed images the user attached, carried
        as ``![name](/api/images/files/..)`` markdown inside the answer text.
        The tool RESULT (``role:tool``) is always pinned to plain text, so the
        model would only ever see the image URL as text — never the pixels.

        For every ``question`` result in the round whose text references one or
        more uploaded images, decode them (via the injected
        :class:`ImageUploadStorePort`) into OpenAI-Vision content blocks and
        append ONE ``{"role":"user","content":[{type:text},{type:image_url}..]}``
        message immediately after this round's ``role:tool`` replies. The next
        LLM round then sees a normal multimodal user turn — the SAME shape the
        user-prompt image path produces (``_streaming_helpers
        .assemble_multimodal_messages``) — so a cloud vision model can read the
        image. No-op when the store is unwired, the round had no ``question``
        result, or no answer referenced a resolvable image: in those cases the
        wire (and thus普通聊天 / the locked tool protocol) is byte-for-byte
        unchanged.

        DESIGN: this is purely ADDITIVE — it never mutates the question's
        ``role:tool`` reply (the tool result text is returned verbatim, so the
        tool-call→tool-result pairing the sanitizer enforces stays intact); it
        only APPENDS an independent user turn. The answer's internal structure
        is not parsed beyond the image-ref regex.
        """
        store = self._image_upload_store
        if store is None or not round_results:
            return
        for tr in round_results:
            if (tr.get("tool_name") or "") != "question":
                continue
            result_text = str(tr.get("result", ""))
            image_refs = _extract_image_refs(result_text)
            if not image_refs:
                continue
            blocks = _resolve_image_refs_to_vision_blocks(
                store=store,
                image_refs=image_refs,
                source_text=result_text,
            )
            if blocks:
                wire_messages.append(
                    {"role": "user", "content": blocks}
                )

    async def _build_llm_request(
        self,
        *,
        conv: Any,
        tab: Any,
        request: "StreamChatInput",
        extra_overrides: dict[str, Any] | None = None,
        compressed_history: tuple[Any, ...] | None = None,
    ) -> LLMStreamRequest:
        extra = dict(request.extra) if request.extra else {}
        if extra_overrides:
            extra.update(extra_overrides)

        # R12 dealign: resolve a selected code persona (id → prompt +
        # display name + groups) into ``extra`` BEFORE tool schemas are
        # collected, so persona-based tool filtering (hard permission
        # isolation) can remove tools the persona should not access, and
        # the system-prompt builder injects it as the active working role.
        # Used to be done after _collect_tool_schemas; moved ahead so
        # the groups are available for tool filtering.
        await self._resolve_code_persona(extra)
        # Propagate persona_groups back to request.extra so the execution
        # gates (_execute_single_tool_call, sub-agent spawn) can enforce
        # persona tool restrictions even though they read request.extra
        # (not the local copy built here for _build_llm_request).
        if "persona_groups" in extra and request.extra is not None:
            request.extra["persona_groups"] = extra["persona_groups"]

        self._collect_tool_schemas(extra, request=request)

        # Sampling-parameter resolution (V1 chat_handler.py:1209-1362 parity).
        # The SSE / WS route layer packs the user's temperature / top_p /
        # max_tokens (+ the optional frequency_penalty / presence_penalty /
        # seed / stop) into ``extra["tool_params"]`` alongside the
        # feature-mode params.  The HTTP transport's ``resolve_params`` reads
        # the *top-level* ``extra`` keys, so without this lift the user's
        # ModelParams panel had NO effect (the sampling overrides never
        # reached the wire — observed as "temperature slider does nothing").
        # This also applies the per-family locks (GPT-5 / o-series force
        # ``temperature=1.0``; DeepSeek-R1 etc.) + the classic ``0.7`` default
        # via the domain ``ModelProfile`` so the cloud payload matches V1.
        self._apply_sampling_params(extra=extra, model_hint=request.model_hint)

        # PR-091 H-4: pre-resolve App Builder Pack catalog before
        # building the system prompt, so the (sync) builder can read
        # the results from ``extra``.  The chat context never imports
        # ``qai.app_builder`` directly; the catalog port is wired via
        # the ``apps/api`` bridge.
        await self._populate_app_builder_catalog(extra=extra)

        # Per-session ("this conversation only") SKILL override for the CLOUD
        # path. The local path filters inside ``_build_available_skills_xml``;
        # the cloud path's skill list comes from the system-prompt builder's
        # ``skill_catalog_provider`` (rows of ``(path, use_for)``), so we
        # pre-resolve it here minus the user's disabled skills and pin the
        # result onto ``extra["skill_catalog"]`` (the builder's highest-priority
        # per-request override). Applied per-turn only — never mutates global
        # forge.config skill mode. No-op when no skills are disabled.
        self._apply_session_skill_override(extra=extra, request=request)

        await self._inject_memory_context(extra=extra, request=request)
        # Resolve any per-conversation workspace override into ``extra`` so the
        # (sync) prompt builder names the SESSION's working directory, not the
        # global default. Must run before ``_apply_system_prompt``.
        await self._resolve_session_workspace(extra=extra, request=request)
        # CLOUD-only: read workspace-root AGENTS.md / CLAUDE.md (best-effort,
        # size-capped) and publish them on ``extra`` so the cloud prompt
        # builder inlines them. Must run after the workspace override is
        # resolved and before ``_apply_system_prompt``.
        await self._resolve_workspace_context_files(extra=extra, request=request)
        self._apply_system_prompt(extra=extra, request=request)

        # Surface the FULLY-built system prompt back onto ``request.extra``
        # so ``_save_prompt_snapshot`` — which reads ``request.extra`` (the
        # original, not this local copy) — captures the EXACT prompt sent to
        # the model instead of rebuilding it from an extra that lacks the
        # app-builder keys (those keys are only set on this local copy). Set
        # once (first round); later followup rounds keep the initial turn's
        # snapshot.
        #
        # R5 fix: previously the writeback only fired when
        # ``isinstance(request.extra, dict)`` — so a turn that started with
        # ``extra is None`` (no tool_mode / app-builder keys) never persisted
        # the built ``system_prompt`` and the snapshot fell back to the
        # rebuild branch.  For the LOCAL path the rebuild branch could not
        # reproduce the minimal prompt (it had no skills) → snapshot ≠ wire.
        # Now, when ``request.extra is None`` we initialise it to a fresh
        # dict via ``object.__setattr__`` (``StreamChatInput`` is frozen +
        # slots, so plain assignment is rejected but ``__setattr__`` on a
        # declared slot is allowed) and then write the prompt so BOTH the
        # wire payload and the snapshot read the identical text (审查 F-1 /
        # R5).
        built_sp = extra.get("system_prompt")
        if isinstance(built_sp, str) and built_sp:
            if request.extra is None:
                object.__setattr__(request, "extra", {})
            req_extra = request.extra
            if isinstance(req_extra, dict) and "system_prompt" not in req_extra:
                req_extra["system_prompt"] = built_sp

        # Snapshot fidelity (debug): besides the system prompt, surface the
        # OTHER request-shaping values the adapters inject just before the
        # wire onto ``request.extra`` so ``_save_prompt_snapshot`` (which reads
        # ``request.extra``) can show the user the REAL payload — the resolved
        # tool schemas (``payload["tools"]``) and the resolved sampling params
        # (temperature / top_p / max_tokens) that the ModelParams panel feeds.
        # These are written under a single ``_snapshot_request_options`` key so
        # they never collide with any wire-bound ``extra`` key and are stripped
        # before sending (``_CHAT_CONTROL_KEYS`` already drops ``__``-prefixed
        # and reserved keys; this key is read only by the snapshot capture).
        # Set once (first round) — later follow-up rounds keep the initial
        # turn's options, matching the per-round snapshot's stable model/mode.
        if isinstance(request.extra, dict) and (
            "_snapshot_request_options" not in request.extra
        ):
            request.extra["_snapshot_request_options"] = (
                self._build_snapshot_request_options(
                    extra=extra, request=request,
                )
            )

        # History excludes the current user turn: ``prompt`` already carries
        # it (the adapters append ``prompt`` after ``history``). ``_prepare_turn``
        # appended the current user message to ``conv`` (line ~979) before this
        # runs, so ``tuple(conv.messages)`` ends with it — passing the full list
        # *and* ``prompt`` would send the user message twice (observed as a
        # duplicated ``<|im_start|>user`` block in the local daemon prompt).
        # Drop the trailing current-user message to mirror V1's single-list
        # semantics. A pre-computed ``compressed_history`` is used as-is.
        if compressed_history is not None:
            history = compressed_history
        else:
            all_messages = tuple(conv.messages)
            history = _drop_trailing_current_user(
                all_messages, current=request.user_message
            )
        # P0-1: WebUI multimodal — when the current user message carries
        # media_refs (image URLs from /api/images/upload), the SSE/WS route
        # should have already resolved them to vision blocks via
        # ``extra["image_content_blocks"]``.  This is a fallback for
        # media_refs that weren't pre-resolved (shouldn't happen in normal
        # flow but defensive).  The route layer handles the actual file I/O.
        if (
            request.user_message.media_refs
            and "image_content_blocks" not in extra
            and "messages" not in extra
        ):
            # Signal to route layer that media_refs were not resolved.
            # In practice the route always resolves them, so this is
            # just a defensive log.
            _log.debug(
                "chat.media_refs_not_preresolved",
                count=len(request.user_message.media_refs),
            )

        self._assemble_multimodal_messages(extra=extra, history=history)

        # P11 — the take-over wire is now constructed ONCE in :meth:`_run`
        # right after :meth:`_maybe_load_subagent_takeover` returns
        # (single source of truth, multimodal-aware via
        # ``_build_base_wire_messages`` + ``_resolved_user_turn``). The
        # previous inline take-over rebuild here (which itself required a
        # ``_is_followup_round`` guard to prevent it from clobbering the
        # follow-up loop's send wire) is removed; ``extra["messages"]``
        # is already populated by ``_run`` on round 0, and the follow-up
        # loop's ``_on_round_open`` overwrites ``extra["messages"]`` with
        # its kernel-grown send wire on subsequent rounds.
        #
        # The private take-over markers must still be POPPED here so they
        # never reach the wire payload (the use case alone consumes them
        # — ``_subagent_takeover_wire`` was used by ``_run`` /
        # ``_build_base_wire_messages``; ``_subagent_takeover`` was used
        # by ``_collect_tool_schemas``).
        extra.pop("_subagent_takeover_wire", None)
        extra.pop("_subagent_takeover", None)
        # ``allow_question`` was consumed by ``_collect_tool_schemas`` (called
        # earlier this turn) — drop it so it never reaches the wire payload.
        extra.pop("allow_question", None)
        # ``self_allow_spawn`` was likewise consumed by ``_collect_tool_schemas``
        # (take-over ``agent`` injection) — drop it so it never reaches the wire
        # payload. ``allow_child_spawn`` is intentionally NOT popped here: it is
        # consumed LATER this turn by ``_dispatch_agent_calls_streaming`` (which
        # runs inside the agentic loop, after this point) to decide whether the
        # sub-agents spawned this turn may themselves spawn.
        extra.pop("self_allow_spawn", None)

        # ── Multi-turn tool-history rebuild (V1 chat_handler.py:878-945) ──
        # When this turn's history contains a PRIOR tool-using round, the
        # adapter's flat ``_normalize_history_message`` would replay it as a
        # bare ``{role, content}`` pair — dropping the historical tool_calls
        # linkage AND leaking the internal ``"[tool_calls]"`` content sentinel
        # to the model (the model then sees garbage text and forgets what it
        # did with tools on earlier turns).  Rebuild the history into the
        # OpenAI wire shape (assistant{tool_calls} + role:tool) and slot it as
        # the adapter's ``extra["messages"]`` override — the SAME mechanism the
        # follow-up loop uses (``_build_base_wire_messages``), so both paths
        # replay tool history identically.  Skipped when an override is already
        # present (multimodal image turn / follow-up round) or when no history
        # message carries tool_calls (the common, no-op case → adapter's flat
        # path is fine and cheaper).
        #
        # Compaction-checkpoint integration (dual-track history): when a
        # session compaction checkpoint exists for this conversation AND the
        # caller did NOT pass an explicit ``compressed_history`` (the initial
        # stream path), assemble the wire from the checkpoint
        # (``compacted_wire + rebuild(history[anchor:])``) instead of the full
        # history — so the SENT wire is the SAME compacted-head + verbatim
        # increment used for estimation/compression (三口径归一), and the
        # already-summarised head is NOT re-sent verbatim each turn.
        _has_checkpoint = (
            compressed_history is None
            and self._compaction_checkpoints.get(self._conv_key(conv)) is not None
        )
        if "messages" not in extra and (
            _has_checkpoint or any(getattr(m, "tool_calls", None) for m in history)
        ):
            if _has_checkpoint:
                rebuilt = self._assemble_history_wire(conv=conv, request=request)
            else:
                rebuilt = _rebuild_history_wire_messages(history)
            rebuilt.append(
                {
                    "role": "user",
                    "content": getattr(request.user_message, "text", "") or "",
                }
            )
            extra["messages"] = rebuilt

        # Single choke point: if ANY branch above assembled an
        # ``extra["messages"]`` override (take-over wire / multimodal /
        # history-rebuild), REPAIR orphan ``role=tool`` rows before it reaches
        # the adapter. Otherwise the pre-send sanitiser would DELETE an orphan
        # on this (and every follow-up) round, stranding the model with a
        # broken context → the reported repeat-the-same-plan infinite loop.
        # Folding (not dropping) keeps the wire valid so nothing is removed
        # round after round. Clean overrides pass through unchanged; no
        # override (the common flat path) is untouched.
        _ov = extra.get("messages")
        if isinstance(_ov, list) and _ov:
            extra["messages"] = _repair_orphan_tool_messages(_ov)

        # prompt_debug (V1 chat_handler.py:414-438 + _log_full_messages): when
        # the operator flag is set for this turn, dump the FULL messages list
        # about to be sent to the model into the backend log. The faithful wire
        # shape is ``extra["messages"]`` when an override was assembled above
        # (history-rebuild / multimodal / takeover); otherwise reconstruct the
        # equivalent ``system + history + current-user`` list the adapter will
        # build, so the dump reflects what the model actually receives. The
        # per-turn flag was stashed on ``request.extra`` by ``_run``.
        if request.extra and request.extra.get("_prompt_debug"):
            channel = (
                "LOCAL" if _is_local_model_hint(request.model_hint) else "CLOUD"
            )
            dump_messages = self._build_prompt_debug_messages(
                extra=extra, history=history, request=request,
            )
            self._log_prompt_debug(
                channel=channel,
                model_id=request.model_hint or "",
                messages=dump_messages,
            )

        return LLMStreamRequest(
            conversation_id=conv.id,
            tab_id=tab.id,
            prompt=request.user_message,
            history=history,
            model_hint=request.model_hint,
            extra=extra or None,
        )

    def _apply_sampling_params(
        self,
        *,
        extra: dict[str, Any],
        model_hint: str | None,
    ) -> None:
        """Lift sampling overrides to top-level ``extra`` + apply family locks.

        C档 阶段1 note (unified收口): the family lock / default backfill is now
        ALSO performed — for EVERY routed request, main agent + sub-agent +
        discussion alike — in ``ProviderRoutingLLMStream._select_target`` ->
        ``_inject_family_sampling_defaults`` (the single收口 point). This main
        agent method is RETAINED on purpose for two reasons it uniquely owns:

        1. **ModelParams panel lift** — copying the user's nested
           ``extra["tool_params"]`` sampling values up to the top level (only the
           main agent has a ModelParams panel; the sub-agent / discussion never
           carry ``tool_params``). The routing layer cannot do this — it reads
           the top-level keys this lift produces.
        2. **Snapshot fidelity** — the resolved temperature / top_p / max_tokens
           must be present in ``extra`` BEFORE :meth:`_build_snapshot_request_options`
           captures them (the user-visible prompt-debug dialog shows the REAL
           wire sampling — State-Truth-First). That capture runs in this
           application layer, before routing, so the values must be resolved here.

        Because ``_inject_family_sampling_defaults`` is idempotent ("a caller
        value is respected; only a family LOCK overrides it; a family DEFAULT
        only fills an absent key"), the routing layer sees the values this method
        already set and is a no-op for the main agent — no double-application,
        no drift. (PENDING-WORK note: this method could not be deleted outright
        without regressing snapshot fidelity; see docs/90-refactor/PENDING-WORK.md.)

        V1 parity (``backend/chat_handler.py:1209-1362``): the user's
        ``ModelParams`` panel values arrive nested inside
        ``extra["tool_params"]`` (the SSE / WS route packs
        ``temperature`` / ``top_p`` / ``max_tokens`` there alongside the
        feature-mode params).  The HTTP transport's
        :func:`~qai.chat.infrastructure.model_param_resolver.resolve_params`
        reads the **top-level** ``extra`` keys, so this method copies the
        seven sampling tunables up to the top level where the adapter can
        see them — fixing the regression where the panel had no effect.

        On top of the lift it applies the per-model **family** policy from
        the domain :class:`~qai.chat.domain.model_profiles.ModelProfile`:

        * ``temperature`` / ``top_p`` family locks
          (GPT-5 / o-series / DeepSeek-R1 require a fixed value or the API
          returns HTTP 400);
        * the classic ``temperature=0.7`` default when the user did not set
          one (matching V1's ``resolve_temperature`` fallback);
        * the family ``max_tokens`` default when none was supplied (the
          runtime-learned ceiling clamp still happens later in the HTTP
          transport's ``_build_payload`` via the ``RuntimeLimitStorePort``).

        The method only depends on the ``domain`` layer (no infrastructure
        import), keeping the ``layered-chat`` contract intact: the adapter's
        resolver then forwards these resolved top-level values verbatim
        (its profile is empty, so it acts as a clamp-only pass-through).

        Existing top-level keys (set directly by a caller) are respected and
        never overwritten by the ``tool_params`` copy; the family lock,
        however, always wins (the API enforces it).
        """
        from qai.chat.domain.model_profiles import get_model_profile

        def _as_float(value: Any) -> float | None:
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        def _as_int(value: Any) -> int | None:
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        tool_params = extra.get("tool_params")
        nested = tool_params if isinstance(tool_params, dict) else {}

        # 1. Lift the seven sampling tunables from tool_params → top level,
        #    without clobbering a value a caller already placed at top level.
        _SAMPLING_KEYS = (
            "temperature",
            "top_p",
            "max_tokens",
            "frequency_penalty",
            "presence_penalty",
            "seed",
            "stop",
        )
        for key in _SAMPLING_KEYS:
            if key not in extra and key in nested:
                extra[key] = nested[key]

        # 2. Apply the per-family policy (locks + classic defaults) so the
        #    cloud payload matches V1.  Best-effort: a profile lookup never
        #    breaks a turn.
        try:
            profile = get_model_profile(model_hint or "")
        except Exception as exc:  # noqa: BLE001 — never crash a turn
            _log.warning("chat.sampling_profile_failed", error=str(exc))
            return

        # temperature: family lock wins; else honour user value; else 0.7.
        resolved_temp = profile.resolve_temperature(
            user_value=_as_float(extra.get("temperature")),
        )
        if resolved_temp is not None:
            extra["temperature"] = resolved_temp

        # top_p: family lock wins; else honour user value; else omit.
        resolved_top_p = profile.resolve_top_p(
            user_value=_as_float(extra.get("top_p")),
        )
        if resolved_top_p is not None:
            extra["top_p"] = resolved_top_p
        elif profile.top_p_fixed is None and "top_p" not in nested and (
            "top_p" not in extra
        ):
            # No user value and no family default → leave unset (API default).
            pass

        # max_tokens: honour user value; else family default.  The runtime
        # ceiling clamp is applied downstream in ``_build_payload``.
        resolved_max_tokens = profile.resolve_max_tokens(
            user_value=_as_int(extra.get("max_tokens")),
        )
        if resolved_max_tokens is not None and resolved_max_tokens > 0:
            extra["max_tokens"] = resolved_max_tokens

    def _collect_tool_schemas(
        self,
        extra: dict[str, Any],
        *,
        request: "StreamChatInput | None" = None,
    ) -> None:
        """Forward the tool registry's OpenAI function-calling schemas.

        PR-fix-cloud-tools (2026-06-04): the cloud-bound HTTP transport
        advertises tools via the standard ``payload["tools"]`` field so
        the model emits native ``tool_calls`` deltas instead of
        XML-text "pretend tool" output.  ``ToolInvocationPort.schemas``
        is an additive method (v2.7 §3.1); called via ``getattr`` so
        legacy / test stubs without it keep working.  No-op when
        ``extra`` already carries ``tools_schemas``.

        Tool-set composition mirrors V1 ``chat_handler.py:389-416``:

        * **Conditional tools** (``appbuilder_run`` / ``appbuilder_batch_run``)
          are advertised ONLY for cloud turns in ``app-builder`` mode. V1
          registers them ``conditional=True`` so the default/local schema list
          omits them (``registry.schemas(exclude_conditional=True)``); they are
          appended as ``extra_tools`` only when ``tool_mode == "app-builder"
          and not is_local`` (``chat_handler.py:402-410``). We replicate that by
          dropping conditional names from the base list and re-adding them only
          for the cloud app-builder case.
        * **The ``agent`` tool** is injected on every depth-0 turn (V1 injects
          it whenever a ``tool_executor`` is present —
          ``chat_handler.py:392-395`` — which is the normal main-agent path,
          local and cloud alike; the live on-device payload audit confirmed the
          9th tool is ``agent``).
        """
        if "tools_schemas" in extra:
            return
        schemas_fn = getattr(self._tools, "schemas", None)
        if not callable(schemas_fn):
            return
        try:
            advertised = schemas_fn()
        except Exception as exc:  # noqa: BLE001 — best-effort
            _log.warning(
                "chat.tool_schemas_collect_failed", error=str(exc),
            )
            advertised = ()

        model_hint = getattr(request, "model_hint", None) if request else None
        is_local = _is_local_model_hint(model_hint)
        tool_mode = extra.get("tool_mode") or extra.get("_effective_tool_mode")

        # Translate the main-agent / take-over composition into the SHARED
        # ``tool_advertise`` helper params (single source of truth — the
        # autonomous sub-agent path ``agent_tool._sub_agent_tool_schemas`` uses
        # the SAME helper, so the two loops can never drift).
        #
        # Main agent (not a take-over): nothing excluded, ``agent`` injected on
        # every depth-0 turn (V1 chat_handler.py:392-395; the live on-device
        # payload audit confirmed the 9th tool is ``agent``).
        #
        # Take-over of a sub-agent (``extra["_subagent_takeover"]``): the user
        # is conversing AS/WITH that sub-agent, so the set matches the
        # autonomous sub-agent set (``SUB_AGENT_EXCLUDED_TOOLS`` =
        # agent / question / list_subagents excluded by default — recursion +
        # background-dialog guards), then per-tab opt-in switches un-exclude:
        #   * ``self_allow_spawn`` → un-exclude ``agent`` AND ``list_subagents``
        #     as a PAIR (口径 unified with the autonomous sub-agent +
        #     main-agent path — ``agent`` needs ``list_subagents`` to look up a
        #     spawned child's id; the taken-over sub-agent runs through THIS
        #     main loop's full ``agent`` dispatch, and its children default to
        #     no further spawn) + inject ``agent`` if the advertised set lacks
        #     it. State-Truth-First: gated on the LIVE take-over context so an
        #     errant flag on a non-take-over turn never changes the set.
        #   * ``allow_question`` → un-exclude ``question`` (its blocking dialog
        #     opt-in for an interactive take-over).
        is_takeover = bool(extra.get("_subagent_takeover"))
        if not is_takeover:
            excluded: frozenset[str] = frozenset()
            inject_agent = True
        else:
            excluded = _SUB_AGENT_EXCLUDED_TOOLS
            self_allow_spawn = bool(extra.get("self_allow_spawn"))
            if self_allow_spawn:
                excluded = excluded - {"agent", "list_subagents"}
            if bool(extra.get("allow_question")):
                excluded = excluded - {"question"}
            inject_agent = self_allow_spawn

        base = _compose_advertised_tools(
            advertised,
            tool_mode=tool_mode,
            is_local=is_local,
            excluded=excluded,
            inject_agent=inject_agent,
            agent_schema_factory=_agent_tool_schema,
            profile=None,
            disabled_tools=_session_disabled_tools(extra)
            | _persona_disabled_tools(extra),
        )

        # Tools-JSON体积压缩 (A1): edit / apply_patch are mutually exclusive on
        # the CLOUD wire. For a
        # non-GPT-family cloud model (Claude / Gemini / ...) drop ``apply_patch``
        # and keep only ``edit`` — the model uses ``edit`` fine and never pays
        # for the second ~1.4 KB schema. GPT-family keeps ``apply_patch``.
        # LOCAL turns are untouched (the on-device path is out of scope here and
        # small models keep their stable set); ``edit`` is ALWAYS retained so a
        # detection miss can only remove apply_patch, never the primary editor.
        if base and not is_local and not _model_hint_prefers_apply_patch(
            model_hint
        ):
            base = [
                s
                for s in base
                if _schema_tool_name(s) != "apply_patch"
            ]

        if base and not is_local:
            _overrides_fn = getattr(
                self._tools, "cloud_description_overrides", None
            )
            if callable(_overrides_fn):
                try:
                    _overrides = _overrides_fn()
                except Exception:  # noqa: BLE001 — best-effort
                    _overrides = {}
                if _overrides:
                    base = _apply_cloud_tool_description_overrides(base, _overrides)

        if base:
            extra["tools_schemas"] = base

    async def _resolve_code_persona(self, extra: dict[str, Any]) -> None:
        """R12: merge a selected code persona into ``extra`` (cross-BC).

        Replicates the legacy ``chat_handler.code_persona_manager``
        behaviour, moved out of the SSE / WS route layer: when
        ``extra["tool_mode"] == "code"`` and ``extra["tool_params"]``
        names a known ``persona`` id, resolve it through the injected
        :class:`~qai.chat.application.ports.CodePersonaResolverPort`
        (wired to the user_prefs context via the ``apps/api`` bridge) and
        write the (override-applied) prompt + display name into
        ``extra["persona"]`` / ``extra["persona_name"]`` so the
        system-prompt builder injects it as the active working role.

        No-op when: the resolver is not wired; the mode is not ``"code"``;
        no persona id is present; a persona was already resolved
        (idempotent across follow-up rounds); or the lookup misses.  The
        chat context never imports ``qai.user_prefs`` — the resolution is
        entirely behind the port.
        """
        if self._code_persona_resolver is None:
            return
        if extra.get("tool_mode") != "code" or "persona" in extra:
            return
        tool_params = extra.get("tool_params")
        if not isinstance(tool_params, dict):
            return
        persona_id = tool_params.get("persona")
        if not isinstance(persona_id, str) or not persona_id.strip():
            return
        resolved = await self._code_persona_resolver.resolve(
            persona_id.strip(), locale=extra.get("locale")
        )
        if resolved is None:
            return
        extra["persona"] = resolved.prompt
        if resolved.name:
            extra["persona_name"] = resolved.name
        if resolved.groups is not None:
            extra["persona_groups"] = list(resolved.groups)

    async def _inject_memory_context(
        self,
        *,
        extra: dict[str, Any],
        request: "StreamChatInput",
    ) -> None:
        """W1-F: inject a ``<past_experiences>`` block into ``extra``.

        When the experience recall port is wired, build a memory-context
        block from the user's latest message and stash it under
        ``extra["memory_context"]`` so the system prompt builder (or the
        downstream LLM adapter) can embed relevant past experiences.
        No-op when the port is absent or ``memory_context`` is preset.
        """
        if self._experience_recall is None or "memory_context" in extra:
            return
        try:
            query_text = (
                request.user_message.text
                if hasattr(request.user_message, "text") and request.user_message.text
                else ""
            )
            if query_text:
                memory_block = await self._experience_recall.build_context_block(
                    query=query_text,
                    max_chars=3000,
                )
                if memory_block.text:
                    extra["memory_context"] = memory_block.text
        except Exception as exc:  # noqa: BLE001 — best-effort
            _log.warning(
                "chat.memory_context_injection_failed", error=str(exc),
            )

    async def _resolve_session_workspace(
        self,
        *,
        extra: dict[str, Any],
        request: "StreamChatInput",
    ) -> None:
        """Resolve the effective workspace root for THIS conversation.

        The system prompt builder is constructed once at DI time with the
        *global* default workspace, so it cannot know a per-conversation
        override on its own. Here (in the async request flow) we read the
        conversation's ``meta.workspace`` and, when set, publish it on
        ``extra["_session_workspace_root"]`` so both the cloud
        (:class:`RichSystemPromptBuilder`) and local prompt paths render the
        session's directory instead of the global default. When the
        conversation has no override the key is left unset and the builder's
        global default applies (V1-style: no per-session workspace).

        Best-effort: any repository error leaves the key unset (→ global
        default) so a storage hiccup never breaks prompt assembly.
        """
        if "_session_workspace_root" in extra:
            return
        conv_id = getattr(request, "conversation_id", None)
        if conv_id is None:
            return
        try:
            conv = await self._conversations.get(conv_id)
        except Exception:  # noqa: BLE001 — never break prompt assembly
            return
        meta = getattr(conv, "meta", None)
        if not isinstance(meta, dict):
            return
        ws = meta.get("workspace")
        if isinstance(ws, str) and ws.strip():
            extra["_session_workspace_root"] = ws.strip()

    async def _resolve_workspace_context_files(
        self,
        *,
        extra: dict[str, Any],
        request: "StreamChatInput",
    ) -> None:
        """Read workspace-root ``AGENTS.md`` / ``CLAUDE.md`` for CLOUD models.

        V2 enhancement (no V1 equivalent for cloud chat): when the resolved
        workspace root contains ``AGENTS.md`` and/or ``CLAUDE.md``, their
        (UTF-8, size-capped) contents are published on
        ``extra["workspace_context_files"]`` as an ordered list of
        ``(filename, content)`` tuples; the cloud prompt builder
        (:class:`RichSystemPromptBuilder`) then inlines each block right after
        the working-directory directive so the model follows the project's
        conventions.

        Scope (per user decision):

        * **Cloud only** — local on-device models (``model_hint`` starts with
          ``local::``) are skipped; CC/OC live in ``qai.ai_coding`` and never
          reach this use case.
        * **Main-agent turns** — this method feeds the MAIN agent's cloud
          prompt builder. Sub-agents build their own minimal prompt in
          ``agent_tool.py:_iter_loop`` and inject the SAME blocks there
          (sharing the reader in ``_workspace_context``), also cloud-only.
        * **Workspace root only** — looks at ``{workspace_root}/<name>``; does
          NOT walk parents or sub-directories.
        * **All cloud modes except translate** — every mode that renders the
          working-directory directive (default / app-builder / feature incl.
          code / ppt / model_build) gets the block; the translate branch of
          the builder returns before any workspace section, so it is excluded
          automatically.

        Must run AFTER :meth:`_resolve_session_workspace` (it reads the
        per-session override published there) and BEFORE
        :meth:`_apply_system_prompt`. Best-effort throughout: a missing file,
        decode error, or any I/O failure simply omits that file and never
        breaks prompt assembly. Read fresh every turn so edits to the files
        take effect immediately (no caching — State-Truth-First).
        """
        # Cloud-only gate: local on-device models do not get this block.
        if _is_local_model_hint(getattr(request, "model_hint", None)):
            return
        if _WORKSPACE_CONTEXT_EXTRA_KEY in extra:
            return

        root_str = self._effective_workspace_root_str(
            extra.get("_session_workspace_root"),
        )

        try:
            root = Path(root_str)
        except (ValueError, OSError):
            return

        resolved = _resolve_workspace_context_files_fn(root)
        if resolved:
            extra[_WORKSPACE_CONTEXT_EXTRA_KEY] = resolved

    def _effective_workspace_root_str(self, session_root: str | None) -> str:
        """Resolve the effective workspace root string (V1-parity fallback).

        Mirrors :meth:`RichSystemPromptBuilder._effective_workspace_root`:
        a non-blank per-session override wins, else the builder's global
        default, else :data:`_DEFAULT_WORKSPACE_ROOT`. Shared by the cloud
        prompt builder path (:meth:`_resolve_workspace_context_files`) AND the
        sub-agent dispatch (:meth:`_dispatch_agent_calls_streaming`) so BOTH
        resolve the workspace identically — a sub-agent inherits the same
        effective root (incl. the global default), not just an explicit
        session override.
        """
        if isinstance(session_root, str) and session_root.strip():
            return session_root.strip()
        builder_default = getattr(
            self._system_prompt_builder, "model_build_workspace_root", None
        )
        return (builder_default or "").strip() or _DEFAULT_WORKSPACE_ROOT

    def _apply_system_prompt(
        self,
        *,
        extra: dict[str, Any],
        request: "StreamChatInput",
    ) -> None:
        """Pre-pend the system prompt into ``extra``.

        Two paths (V1 parity ``backend/chat_handler.py:414-433``):

        * **Local (on-device) model** (``model_hint`` starts with
          ``local::``) — builds a MINIMAL prompt via
          :meth:`_build_local_system_prompt`: the front-end base system
          (if any) + ``agent=main`` + ``<available_skills>`` metadata XML.
          No cloud catalog / few-shot / Python-env / app-builder catalog,
          and **no model-build auto-detection** (V1 chat_handler.py:383
          gates auto-detect on ``not is_local``).  The on-device model
          reads each ``<location>`` SKILL.md on demand.
        * **Cloud model** — delegates to the rich
          :class:`SystemPromptBuilderPort` (full catalogue + auto-detect).

        No-op when ``extra`` already carries a ``system_prompt``.  Also
        propagates ``_effective_tool_mode`` so the caller can detect
        auto-detection promotions and emit ``tool_mode_changed`` frames
        (local never auto-promotes, so it leaves the mode unchanged).
        """
        if "system_prompt" in extra:
            return

        # ── V1 parity: local vs cloud dispatch (chat_handler.py:400) ──
        if _is_local_model_hint(request.model_hint):
            local_prompt = self._build_local_system_prompt(
                extra=extra, request=request,
            )
            if local_prompt:
                extra.setdefault("system_prompt", local_prompt)
            # Local path NEVER auto-detects a tool_mode (V1
            # chat_handler.py:383 ``not is_local``); leave
            # ``_effective_tool_mode`` untouched so no spurious
            # ``tool_mode_changed`` frame is emitted.
            return

        if self._system_prompt_builder is None:
            return
        try:
            # Ensure the auto-detect guard sees the latest user message.
            if "latest_user_message" not in extra:
                _latest_msg = getattr(request.user_message, "text", None)
                if isinstance(_latest_msg, str) and _latest_msg:
                    extra["latest_user_message"] = _latest_msg
            sp_result = self._system_prompt_builder.build(
                SystemPromptRequest(
                    tool_mode=extra.get("tool_mode"),
                    tool_params=extra.get("tool_params"),
                    extra=extra,
                ),
            )
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("chat.system_prompt_build_failed", error=str(exc))
            sp_result = SystemPromptResult(prompt="", effective_tool_mode=None)
        if sp_result.prompt:
            extra.setdefault("system_prompt", sp_result.prompt)
        if sp_result.effective_tool_mode is not None:
            extra.setdefault(
                "_effective_tool_mode", sp_result.effective_tool_mode,
            )

    def _build_local_system_prompt(
        self,
        *,
        extra: dict[str, Any],
        request: "StreamChatInput",
    ) -> str:
        """Assemble the V1-style minimal system prompt for a local model.

        V1 parity (``backend/chat_handler.py:952-1055
        _build_local_messages`` + ``backend/local_prompt_builder.py:95-129
        LocalPromptBuilder.build_messages`` + ``backend/skill_manager.py:
        390-421 build_available_skills_xml``):

        ``[<base system>\\n\\n]agent=main[\\n\\n<available_skills>…]``

        where ``<base system>`` is the front-end-supplied system identity
        (V2 carries it on ``extra["base_system_prompt"]`` if the route
        layer set one; otherwise empty — V1's ``_build_local_messages``
        likewise injects nothing of its own beyond the marker + skills).
        Complex tool modes (app-builder / model-build) still get ONLY the
        default skills metadata XML — V1 chat_handler.py:979-992 skips
        SKILL injection for on-device models entirely.
        """
        # Base system identity (front-end supplied), if any.  V1's
        # LocalPromptBuilder.build_messages merges an existing messages[0]
        # ``system`` content with the injection; V2 surfaces that base via
        # ``extra["base_system_prompt"]`` (empty when the front-end sent
        # none — the common case for the on-device chat path).
        base = extra.get("base_system_prompt")
        base_text = base.strip() if isinstance(base, str) else ""

        # ``<available_skills>`` metadata XML for LOCAL-visible skills.
        skills_xml = self._build_available_skills_xml(extra)

        # Working-directory directive (same workspace semantics as the cloud
        # path). The on-device prompt does not flow through
        # ``RichSystemPromptBuilder``, so inject it here too. Prefer the
        # per-conversation override resolved into ``extra``; else read the
        # resolved global workspace off the injected builder (duck-typed; no
        # adapter import); else the canonical default.
        session_ws = extra.get("_session_workspace_root")
        if isinstance(session_ws, str) and session_ws.strip():
            workspace_root: str = session_ws.strip()
        else:
            _builder_ws = getattr(
                self._system_prompt_builder, "model_build_workspace_root", None
            )
            workspace_root = _builder_ws if isinstance(_builder_ws, str) else ""
        workspace_directive = _build_local_workspace_directive(workspace_root)

        parts: list[str] = [_LOCAL_AGENT_MAIN_MARKER, workspace_directive]
        if skills_xml:
            parts.append(skills_xml)
        # Language-follow rule (on-device counterpart of the cloud
        # LANGUAGE_RULE_GUIDANCE; see _LOCAL_LANGUAGE_RULE).
        parts.append(_LOCAL_LANGUAGE_RULE)
        injection = "\n\n".join(parts)

        if base_text:
            return f"{base_text}\n\n{injection}"
        return injection

    def _build_available_skills_xml(
        self, extra: dict[str, Any] | None = None
    ) -> str:
        """Render the ``<available_skills>`` XML from local-visible skills.

        V1 parity ``backend/skill_manager.py:390-421
        build_available_skills_xml`` (model_type="local").  Pulls
        ``((skill_id, use_for, path), ...)`` triples from the wired
        ``local_skill_catalog_provider`` and renders one ``<skill>``
        element per row with XML-escaped ``<name>`` / ``<description>`` /
        ``<location>``.  Returns ``""`` when no provider is wired or no
        local-visible skill exists (V1 returns ``""`` likewise).

        Per-session override (V2 enhancement): ``extra["disabled_skills"]`` —
        skill ids the user switched OFF for THIS session — are dropped from the
        rendered list (applied per-turn only; never mutates global forge.config
        skill mode). Empty / absent ⇒ the unchanged full local-visible set.
        """
        if self._local_skill_catalog_provider is None:
            return ""
        try:
            rows = self._local_skill_catalog_provider()
        except Exception as exc:  # noqa: BLE001 — best-effort
            _log.warning("chat.local_skill_catalog_failed", error=str(exc))
            return ""
        if not rows:
            return ""
        disabled_skills: set[str] = set()
        if isinstance(extra, dict):
            raw = extra.get("disabled_skills")
            if isinstance(raw, (list, tuple)):
                disabled_skills = {str(n) for n in raw if n}
        import html as _html

        lines = ["<available_skills>"]
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                continue
            skill_id, use_for, path = str(row[0]), str(row[1]), str(row[2])
            if skill_id in disabled_skills:
                continue
            lines.append("  <skill>")
            lines.append(f"    <name>{_html.escape(skill_id)}</name>")
            lines.append(f"    <description>{_html.escape(use_for)}</description>")
            lines.append(f"    <location>{_html.escape(path)}</location>")
            lines.append("  </skill>")
        lines.append("</available_skills>")
        # If every skill was filtered out, return "" so the local prompt omits
        # the empty block entirely (matches the "no skills" path above).
        if len(lines) == 2:
            return ""
        return "\n".join(lines)


    @staticmethod
    def _assemble_multimodal_messages(
        *,
        extra: dict[str, Any],
        history: tuple[Any, ...],
    ) -> None:
        """PR-097 K-1: assemble the multimodal LLM message list in ``extra``.

        Thin wrapper over
        :func:`_streaming_helpers.assemble_multimodal_messages` (ARCH-1 /
        A-3 cohesion split): the function never used ``self`` / ``cls``;
        logic byte-for-byte unchanged.
        """
        _assemble_multimodal_messages_fn(extra=extra, history=history)

    async def _populate_app_builder_catalog(
        self,
        *,
        extra: dict[str, Any],
    ) -> None:
        """Resolve App Builder Pack metadata into ``extra`` for the prompt builder.

        Reads ``extra["tool_mode"]`` / ``extra["tool_params"]`` and (when
        ``tool_mode == "app-builder"`` and the
        :class:`AppBuilderSkillCatalogPort` is wired on the system
        prompt builder) calls the port to fetch:

        * SKILL file paths to inline (``app_builder_skill_files`` →
          tuple[str, ...]), and
        * the Pack catalog Markdown block (``app_builder_pack_catalog``
          → str).

        These are then consumed by
        :meth:`RichSystemPromptBuilder._build_app_builder_prompt`.
        Reference: legacy ``backend/chat_handler.py:3003-3053``.
        """
        builder = self._system_prompt_builder
        if builder is None:
            return
        tool_mode = extra.get("tool_mode")
        if tool_mode != "app-builder":
            return
        catalog_port = getattr(builder, "app_builder_skill_catalog", None)
        if catalog_port is None:
            return
        # Don't overwrite values the caller already provided.
        if "app_builder_skill_files" not in extra:
            try:
                tool_params = extra.get("tool_params")
                if not isinstance(tool_params, dict):
                    tool_params = None
                files = await catalog_port.resolve_skill_files(
                    tool_mode=tool_mode,
                    tool_params=tool_params,
                )
                extra["app_builder_skill_files"] = tuple(files) if files else ()
            except Exception as exc:  # noqa: BLE001 — best-effort
                _log.warning(
                    "chat.app_builder_skill_files_failed",
                    error=str(exc),
                )
                extra["app_builder_skill_files"] = ()
        if "app_builder_pack_catalog" not in extra:
            try:
                catalog_text = await catalog_port.generate_pack_catalog_prompt()
                extra["app_builder_pack_catalog"] = catalog_text or ""
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "chat.app_builder_pack_catalog_failed",
                    error=str(exc),
                )
                extra["app_builder_pack_catalog"] = ""
        # Multi-model inference code (runner.py, capped) for the selected
        # model(s). Optional port method — degrade to no-code when the
        # adapter predates it. Consumed by
        # ``RichSystemPromptBuilder._build_app_builder_prompt``.
        if "app_builder_model_code" not in extra:
            resolve_code = getattr(
                catalog_port, "resolve_model_inference_code", None
            )
            if resolve_code is None:
                extra["app_builder_model_code"] = ()
            else:
                try:
                    tool_params = extra.get("tool_params")
                    if not isinstance(tool_params, dict):
                        tool_params = None
                    blocks = await resolve_code(tool_params=tool_params)
                    extra["app_builder_model_code"] = (
                        tuple(blocks) if blocks else ()
                    )
                except Exception as exc:  # noqa: BLE001 — best-effort
                    _log.warning(
                        "chat.app_builder_model_code_failed",
                        error=str(exc),
                    )
                    extra["app_builder_model_code"] = ()

    def _apply_session_skill_override(
        self,
        *,
        extra: dict[str, Any],
        request: "StreamChatInput",
    ) -> None:
        """Filter the CLOUD skill catalog by the per-session disabled skills.

        The cloud system-prompt builder resolves its skill list from
        ``skill_catalog_provider`` (rows of ``(path, use_for)``) UNLESS
        ``extra["skill_catalog"]`` is set (its highest-priority per-request
        override — see ``RichSystemPromptBuilder._resolve_skill_catalog``). To
        honour the user's per-session "turn this skill off for this
        conversation" choice on the cloud path, we pre-resolve the provider
        rows here, drop those whose skill id is in ``extra["disabled_skills"]``,
        and pin the filtered list onto ``extra["skill_catalog"]``.

        Skill id ⇄ row mapping: a catalog ``path`` is ``…/<skill_id>/SKILL.md``
        (the directory name IS the skill id — the same id the front-end skills
        store and ``LocalChatSkillCatalogProvider`` use), so the id is the
        parent directory's basename of the path.

        No-ops (leaving cloud behaviour byte-for-byte unchanged) when:
          * there are no disabled skills,
          * this is a LOCAL turn (the local path filters in
            ``_build_available_skills_xml`` instead),
          * a ``skill_catalog`` override is already present (respect the
            caller / app-builder branch), or
          * no live cloud ``skill_catalog_provider`` is wired.

        Applied per-turn only; never mutates global forge.config skill mode.
        """
        raw = extra.get("disabled_skills")
        if not isinstance(raw, (list, tuple)) or not raw:
            return
        disabled = {str(n) for n in raw if n}
        if not disabled:
            return
        if _is_local_model_hint(getattr(request, "model_hint", None)):
            return
        if "skill_catalog" in extra:
            return
        builder = self._system_prompt_builder
        provider = getattr(builder, "skill_catalog_provider", None) if builder else None
        if not callable(provider):
            return
        try:
            rows = provider()
        except Exception as exc:  # noqa: BLE001 — best-effort
            _log.warning("chat.cloud_skill_catalog_failed", error=str(exc))
            return
        if not rows:
            return
        import os as _os

        filtered: list[tuple[str, str]] = []
        for row in rows:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                path, use_for = str(row[0]), str(row[1])
            elif isinstance(row, dict):
                path = str(row.get("path", ""))
                use_for = str(row.get("use_for") or row.get("description") or "")
            else:
                continue
            if not path:
                continue
            # Skill id = the SKILL.md file's parent directory name.
            skill_id = _os.path.basename(_os.path.dirname(path))
            if skill_id in disabled:
                continue
            filtered.append((path, use_for))
        # Pin the filtered catalog as the per-request override (even when empty:
        # an explicit empty tuple means "no cloud skills this turn", which is the
        # correct result if the user disabled all of them).
        extra["skill_catalog"] = tuple(filtered)

    # ── Session-level compaction checkpoint (dual-track history) ──────────
    # ``conv.messages`` is NEVER mutated by compaction (it stays the full
    # original history for the user).  Instead a per-conversation in-memory
    # ``CompactionCheckpoint`` records "as of original message N, the
    # compacted wire is X"; the wire SENT to the model is then
    # ``checkpoint.compacted_wire + rebuild(history[N:])``.  The estimate that
    # GATES compression, the wire fed to the COMPRESSOR, and the wire SENT to
    # the LLM are the SAME含-tool-output wire (三口径归一).

    @property
    def _compaction_checkpoints(self) -> dict[str, _CompactionCheckpoint]:
        """Proxy to the engine's in-memory checkpoint cache (same dict object).

        Kept as a property so every existing SYNC reader
        (``_assemble_history_wire`` / ``estimate_compacted_tokens`` / the
        compact hook / tests) keeps working unchanged after the engine
        extraction — they read/pop/assign through the SAME underlying dict the
        engine writes through, so behaviour is byte-for-byte identical.
        """
        return self._compaction_engine.checkpoints

    @property
    def _compaction_checkpoint_loaded(self) -> set[str]:
        """Proxy to the engine's lazy-load memo set (same set object)."""
        return self._compaction_engine._loaded

    @staticmethod
    def _conv_key(conv: Any) -> str:
        """Stable string key for the per-conversation checkpoint dict."""
        cid = getattr(conv, "id", None)
        return str(getattr(cid, "value", cid) if cid is not None else id(conv))

    # ------------------------------------------------------------------
    # CCD-5: compaction-checkpoint durable persistence (write-through cache).
    #
    # The in-memory ``_compaction_checkpoints`` dict is the source of truth
    # within a process; these helpers mirror it to/from the optional
    # ``CompactionCheckpointStorePort`` so a checkpoint survives a restart.
    # When no store is wired they are no-ops, so the pure-memory behaviour is
    # byte-for-byte unchanged (judgement 2: no regression). All persistence is
    # best-effort — a store failure NEVER breaks a turn (the in-memory cache
    # still works; we only lose the durability tier).
    # ------------------------------------------------------------------
    @staticmethod
    def _conv_id_for_key(conv: Any) -> ConversationId | None:
        """Resolve a ``ConversationId`` for the store from a conv-like object.

        Returns the conv's own ``id`` when it is a ``ConversationId``; else
        coerces its ``.value`` (or the bare object) through ``ConversationId.of``.
        ``None`` when no usable id exists (e.g. an anonymous object keyed by
        ``id(conv)`` — such a conv has no durable identity, so persistence is
        skipped for it).
        """
        cid = getattr(conv, "id", None)
        if cid is None:
            return None
        if isinstance(cid, ConversationId):
            return cid
        raw = getattr(cid, "value", cid)
        try:
            return ConversationId.of(str(raw))
        except Exception:  # noqa: BLE001
            return None

    async def _persist_checkpoint(
        self, conv: Any, checkpoint: "_CompactionCheckpoint",
    ) -> None:
        """Write a checkpoint through to the durable store (best-effort)."""
        await self._compaction_engine.persist(
            self._conv_id_for_key(conv), checkpoint,
        )

    async def _drop_persisted_checkpoint(
        self, conversation_id: ConversationId,
    ) -> None:
        """Delete a checkpoint from the durable store (best-effort)."""
        await self._compaction_engine.drop_persisted(conversation_id)

    async def _lazy_load_checkpoint(self, conv: Any) -> None:
        """Populate the in-memory checkpoint for ``conv`` from sqlite (once).

        Delegates to :class:`CompactionCheckpointEngine`. Called from the async
        turn preamble (``_prepare_turn``) so downstream SYNC readers
        (``_assemble_history_wire`` / ``_build_base_wire_messages`` /
        ``estimate_compacted_tokens``) hit the in-memory dict. At most once per
        conversation per process; never overwrites a newer in-memory copy.
        """
        await self._compaction_engine.lazy_load(
            self._conv_key(conv), self._conv_id_for_key(conv),
        )

    async def _resolve_compaction_ratios(self) -> dict[str, float]:
        """Resolve the user-chosen compaction ratios for this compaction.

        Delegates to :class:`CompactionCheckpointEngine.resolve_ratios` (which
        reads the injected ratio provider, clamps each value to ``[0.01, 1.0]``,
        caps ``protect`` at ``target``, and falls back to the kernel defaults
        0.35 / 0.35 when unwired — byte-for-byte the prior behaviour).
        """
        return await self._compaction_engine.resolve_ratios()

    def _history_messages(self, *, conv: Any, request: "StreamChatInput") -> tuple[Any, ...]:
        """The conversation history WITHOUT the trailing current-user turn.

        Mirrors ``_build_base_wire_messages``'s base: the trailing current
        user message is re-appended by the wire builder, so compaction works
        over the history that PRECEDES it and the checkpoint anchor indexes
        into this same dropped list.
        """
        return _drop_trailing_current_user(
            tuple(conv.messages), current=request.user_message
        )

    @staticmethod
    def _max_history_rounds_override(request: "StreamChatInput") -> int | None:
        """Return the per-user ``/compact <N>`` round cap, if explicitly set.

        Channels forward the override as ``StreamChatInput.extra``
        ``["max_history_rounds"]`` (set by
        ``apps/api/_chat_message_bridge.set_max_history_rounds`` on a
        ``/compact <N>`` command — V1 ``_user_max_history_rounds`` parity).
        Returns ``None`` when absent / non-positive, in which case NO
        round-based trimming is applied (plain token compaction governs — a
        V2 upgrade over V1's unconditional cap; the user must explicitly opt
        in via ``/compact N``).
        """
        extra = request.extra if isinstance(request.extra, dict) else None
        if not extra:
            return None
        raw = extra.get("max_history_rounds")
        if isinstance(raw, bool):  # bool is an int subclass — reject it
            return None
        if isinstance(raw, int) and raw > 0:
            return raw
        return None

    def _assemble_history_wire(
        self, *, conv: Any, request: "StreamChatInput"
    ) -> list[dict[str, Any]]:
        """Assemble the current full history wire (含 role:tool outputs).

        Uses the session checkpoint when present: the already-summarised head
        (``checkpoint.compacted_wire``, NOT recompressed) + the verbatim
        increment that accrued past ``anchor_index`` rebuilt into wire shape.
        Without a checkpoint, rebuilds the entire history.  This is the SAME
        wire used for estimation, compression input, and (via
        ``_build_base_wire_messages``) the outbound LLM payload.
        """
        history = self._history_messages(conv=conv, request=request)
        ckpt = self._compaction_checkpoints.get(self._conv_key(conv))
        if ckpt is not None and 0 <= ckpt.anchor_index <= len(history):
            increment = _rebuild_history_wire_messages(history[ckpt.anchor_index:])
            return [dict(m) for m in ckpt.compacted_wire] + increment
        return _rebuild_history_wire_messages(history)

    def estimate_compacted_tokens(
        self, conversation: Any, model_id: str | None,
    ) -> int | None:
        """Probe the conversation's POST-compaction context-token count.

        Read-only, best-effort, never raises (returns ``None`` on any miss
        or failure). Drives the ``/context`` endpoint's compression badge:

        * ``None`` — this conversation has NO session compaction checkpoint
          (it was never compressed), so the "压缩后" figure is undefined and
          the badge stays in its un-compressed state.
        * ``int`` — the conversation HAS a checkpoint; this is the
          best-known token count of the compacted wire that is ACTUALLY sent
          to the model now.

        Cloud-first token accounting (replaces the prior local tiktoken
        rebuild-and-count): after compaction, the NEXT real turn's prompt is
        the compacted wire, and the provider measures it exactly and returns
        ``usage.last_round_prompt_tokens`` on that turn's assistant message.
        That key is the TRUE wire size of the turn's last round (vs the
        cross-round SUM in ``prompt_tokens``, which over-counts multi-round
        turns), so it is the most accurate compacted-wire size — prefer it,
        falling back to ``prompt_tokens`` when the last-round figure is absent.

        CCD-1 (PENDING-WORK.md §1): ``_CompactionCheckpoint`` now carries
        ``anchor_message_id`` (the id of ``conv.messages[anchor_index-1]``, set
        at checkpoint creation), so we DO the strict "assistant turn ran AFTER
        the checkpoint" filter: ``_last_assistant_with_usage`` is called with
        ``after_message_id=ckpt.anchor_message_id`` and skips every message up
        to and including the anchor — so a pre-compaction assistant's
        (typically much larger) ``last_round_prompt_tokens`` can NEVER be
        mistaken for the post-compaction wire size during the window between
        checkpoint creation and the first post-compaction usable assistant
        landing. When no post-anchor assistant-with-usage exists yet, fall back
        to the char-based estimate stashed at checkpoint-creation time.
        State-Truth-First (AGENTS.md 铁律 1): cloud usage is the real measured
        state; the stashed estimate is only a bootstrap value before the first
        post-compaction turn lands. (``created_at`` is also carried but is a
        diagnostic field only — the id-based anchor filter is the authoritative
        before/after discriminator, robust to clock skew / restore-from-disk.)
        """
        ckpt = self._compaction_checkpoints.get(self._conv_key(conversation))
        if ckpt is None:
            # DIAG (token-display investigation): no checkpoint → badge must
            # stay un-compacted (returns None). If the UI shows a "压缩后/省%"
            # state while this logs None, the bug is on the display side.
            _log.info(
                "chat.diag.estimate_compacted",
                conversation_id=getattr(
                    getattr(conversation, "id", None), "value", None
                ),
                has_checkpoint=False,
                result=None,
            )
            return None
        # Prefer cloud truth: if a turn ran AFTER this checkpoint, the last
        # assistant's last_round_prompt_tokens IS the true compacted-wire size
        # the provider measured (prompt_tokens is the cross-round SUM, inflated
        # for multi-round turns).
        # CCD-1 (PENDING-WORK.md §1): only consider assistant messages
        # STRICTLY AFTER the checkpoint's anchor. Without this filter, the
        # brief window between checkpoint creation and the first post-
        # compaction usage block lets the PRE-compaction assistant (with its
        # much larger ``last_round_prompt_tokens``) leak into the badge and
        # display a falsely-large "压缩后" figure. When the filter rules out
        # every candidate (no post-compaction usage yet) we fall back to the
        # stashed char-bootstrap estimate — the same bootstrap window the
        # un-anchored path used at compaction-creation time.
        last_asst = _last_assistant_with_usage(
            conversation, after_message_id=ckpt.anchor_message_id,
        )
        _diag_usage: dict[str, Any] | None = None
        _diag_pt = 0
        if last_asst is not None:
            _u = last_asst.usage or {}
            _diag_usage = dict(_u) if isinstance(_u, dict) else None
            pt = int(_u.get("last_round_prompt_tokens") or _u.get("prompt_tokens") or 0)
            _diag_pt = pt
            if pt > 0:
                # DIAG: the "压缩后/7.1K" figure source — last assistant's
                # last_round_prompt_tokens. After the _extract_usage fix this is
                # the CORRECTED prompt size; if it is STILL tiny (~7.1K) on a
                # 216K-occupancy conversation, the bug is that the checkpoint's
                # last assistant turn used a small wire (the trigger口径 vs the
                # full-history口径 diverge) — surface BOTH the per-round and the
                # cross-round sum so we can tell which the wire really sent.
                _log.info(
                    "chat.diag.estimate_compacted",
                    conversation_id=getattr(
                        getattr(conversation, "id", None), "value", None
                    ),
                    has_checkpoint=True,
                    source="last_assistant_prompt_tokens",
                    last_asst_usage=_diag_usage,
                    last_round_prompt_tokens=_u.get("last_round_prompt_tokens"),
                    prompt_tokens=_u.get("prompt_tokens"),
                    total_tokens=_u.get("total_tokens"),
                    result=pt,
                    ckpt_estimated_tokens=ckpt.estimated_tokens,
                    ckpt_last_eff_prompt=ckpt.last_eff_prompt,
                )
                return pt
        _log.info(
            "chat.diag.estimate_compacted",
            conversation_id=getattr(
                getattr(conversation, "id", None), "value", None
            ),
            has_checkpoint=True,
            source="ckpt_estimated_tokens",
            last_asst_usage=_diag_usage,
            last_asst_pt=_diag_pt,
            result=ckpt.estimated_tokens,
            ckpt_last_eff_prompt=ckpt.last_eff_prompt,
        )
        return ckpt.estimated_tokens

    def invalidate_compaction_checkpoint(
        self, conversation_id: ConversationId,
    ) -> bool:
        """Drop the IN-MEMORY compaction checkpoint for ``conversation_id``.

        **Future use** (CCD-3 from PENDING-WORK.md §1): when chat acquires
        edit / delete / regenerate / rewind routes that mutate
        ``conv.messages`` BEFORE the checkpoint's ``anchor_index``, the
        stale ckpt's ``compacted_wire + history[anchor:]`` will produce a
        corrupt wire (the increment now starts at a different message than
        what the compacted head encoded). The mutating UseCase MUST call
        this method to drop the checkpoint; the next turn will rebuild
        from scratch.

        This method is INTENTIONALLY NOT CALLED by any current route — chat
        has no edit/delete/regenerate/rewind routes today (verified by grep).
        It is shipped now (CCD-3) so the future PR that adds those routes
        has a single, well-documented hook to wire into rather than
        re-discovering the staleness trap.

        CCD-5: this drops ONLY the in-memory cache (kept SYNC so existing
        callers / tests are unchanged). A mutating route running in an async
        context that ALSO needs the durable copy dropped should call
        :meth:`invalidate_compaction_checkpoint_async` instead (it drops both
        memory and sqlite). Leaving a now-stale persisted row is safe: the
        next turn lazy-loads it, then the first compaction overwrites it (the
        in-memory cache is authoritative) — and a conversation DELETE removes
        it via the table's ``ON DELETE CASCADE`` regardless.

        Returns ``True`` if a checkpoint existed and was dropped, ``False``
        otherwise (idempotent for the "nothing to drop" case — safe to call
        unconditionally from a mutating route).
        """
        key = str(getattr(conversation_id, "value", conversation_id))
        existed = key in self._compaction_checkpoints
        if existed:
            self._compaction_checkpoints.pop(key, None)
        # CCD-5: forget the lazy-load memo so a subsequent turn re-loads the
        # (possibly future-rewritten) persisted copy rather than trusting the
        # now-dropped in-memory state.
        self._compaction_checkpoint_loaded.discard(key)
        _log.info(
            "chat.compaction.invalidated",
            conversation_id=key,
            existed=existed,
        )
        return existed

    async def invalidate_compaction_checkpoint_async(
        self, conversation_id: ConversationId,
    ) -> bool:
        """Async variant of :meth:`invalidate_compaction_checkpoint` (CCD-5).

        Drops BOTH the in-memory cache AND the durable store row, so a future
        mutating route running in an async context can fully invalidate the
        checkpoint (no stale persisted copy lingers). The durable delete is
        best-effort (never raises — a store failure is logged and ignored).
        Returns the same ``bool`` as the sync variant (whether an in-memory
        checkpoint existed).
        """
        existed = self.invalidate_compaction_checkpoint(conversation_id)
        cid = (
            conversation_id
            if isinstance(conversation_id, ConversationId)
            else ConversationId.of(
                str(getattr(conversation_id, "value", conversation_id))
            )
        )
        await self._drop_persisted_checkpoint(cid)
        return existed

    async def ensure_compaction_checkpoint_loaded(self, conv: Any) -> None:
        """Public async hook to lazy-load a conversation's durable checkpoint.

        CCD-5 (State-Truth-First, AGENTS.md 铁律 1): the SYNC
        :meth:`estimate_compacted_tokens` (badge probe) reads ONLY the
        in-memory ``_compaction_checkpoints`` cache. Inside a chat turn that
        cache is populated by ``_prepare_turn`` → ``_lazy_load_checkpoint``.
        But the ``/context`` badge endpoint runs OUTSIDE a turn (it fires on
        tab open / model switch), so on a fresh process — or after the user
        reopens a tab in a newly-started backend — the in-memory cache is
        empty and the probe would report ``has_checkpoint=False`` (the badge
        shows "uncompressed") even though a compacted checkpoint is durably
        persisted in sqlite. That makes the UI claim the conversation is NOT
        compressed and look like it will recompress from scratch.

        This method lets any async read-path (the badge endpoint) restore the
        durable checkpoint into the in-memory cache BEFORE calling the sync
        ``estimate_compacted_tokens`` — so the badge reflects the REAL persisted
        state, not a cold-cache illusion. It is the same lazy-load the turn
        preamble uses (at most once per conversation per process, never
        overwriting a newer in-memory copy), exposed as a public seam so the
        interfaces layer does not reach into a private method. No-op when no
        durable store is wired (pure-memory mode unchanged).
        """
        await self._lazy_load_checkpoint(conv)

    def _presend_eff_estimate(
        self,
        *,
        conv: Any,
        request: "StreamChatInput",
        model_id: str,
    ) -> int:
        """Estimate the ``实发`` size for the presend / retry trigger path.

        ``实发 = (last assistant's measured eff_prompt) + (this turn's NEW user
        message tokens)``. The last assistant's ``eff_prompt`` (provider-
        measured ``last_round_prompt_tokens`` +cache_read for Anthropic, via the
        shared :func:`assistant_eff_prompt`口径) IS the size of everything sent
        up to and including the previous turn — already含 the compaction head +
        all prior rounds + system/tools. Adding only the NEW user message gives
        the size the NEXT request will actually send. When no prior assistant
        usage exists (brand-new / local-only history) the prior part is 0.

        New-user-message sizing:
          * ``len(text) > 2000`` → precise BPE (``precise_text_tokens``); on
            tiktoken unavailability falls back to ``len(text)//2``.
          * otherwise → ``len(text)//2``.

        Best-effort, never raises.
        """
        prior_eff = 0
        try:
            last_asst = _last_assistant_with_usage(conv)
            if last_asst is not None:
                # CCD-2 (PENDING-WORK.md §1): the usage came from
                # ``last_asst`` — judge the cache-read add-back rule against
                # THAT message's source model id, NOT the current request's
                # ``model_id``. After a model switch (Claude → GPT) the
                # historical Claude usage still carries Claude's cache-read
                # split-out and must add it back; the current GPT id would
                # incorrectly skip the add-back. Fall back to ``model_id``
                # when the historical message lacks ``model_id`` (legacy
                # rows / non-cloud turns).
                _source_model_id = (
                    getattr(last_asst, "model_id", None) or model_id
                )
                prior_eff = _assistant_eff_prompt(
                    last_asst.usage or {},
                    _is_anthropic_family(_source_model_id),
                )
        except Exception:  # noqa: BLE001 — never break the trigger estimate
            prior_eff = 0

        text = getattr(request.user_message, "text", None) or ""
        if len(text) > 2000:
            precise = _precise_text_tokens(text, model_id)
            new_user = precise if precise is not None else len(text) // 2
        else:
            new_user = len(text) // 2
        return int(prior_eff) + int(new_user)

    async def _compress_via_checkpoint(
        self,
        *,
        conv: Any,
        request: "StreamChatInput",
        target_ratio: float,
        force: bool = False,
        live_wire: list[dict[str, Any]] | None = None,
        measured_eff_prompt: int | None = None,
        live_completed_rounds: list["_CompletedRound"] | None = None,
    ) -> bool:
        """Compress the assembled history wire into a new session checkpoint.

        Estimates the assembled wire (BPE-free chars/4口径 via
        :func:`estimate_wire_tokens`, which counts content + tool_calls
        args).  When the estimate exceeds
        ``INTER_ROUND_COMPRESS_THRESHOLD_RATIO × context_limit`` (or ``force``
        is set, used by the prompt-too-long retry where the provider already
        rejected the prompt), compresses the FULL assembled wire and stores it
        as a new checkpoint anchored at the current history length.

        ``live_wire`` (mid-turn enhancement): when supplied, the wire that is
        estimated + compressed is THIS exact list (the follow-up loop's live,
        authoritative ``wire_messages`` — ``system-less base + current user +
        every in-flight tool round block``) instead of the conv-derived
        ``_assemble_history_wire``.  This is what lets a SINGLE super-long
        agentic turn (e.g. model-build's 100+ tool round-trips, none of which
        is the *next* persisted turn yet) be compressed mid-turn: the
        in-flight rounds live only in the loop's ``wire_messages`` /
        ``completed_rounds`` and are not guaranteed to be in ``conv.messages``
        (incremental persistence is best-effort), so we compress the loop's
        own truth source rather than re-deriving from conv.  The stored
        ``compacted_wire`` then already INCLUDES those in-flight rounds, and
        the caller MUST clear its ``completed_rounds`` so it does not replay
        them on top (that replay-after-fold is the one duplication trap — see
        ``_run_followup_loop``).  Cross-turn / presend / retry paths pass
        ``None`` and keep deriving from conv (unchanged, already correct).

        The checkpoint ``anchor_index`` is ALWAYS ``len(history)`` (the
        trailing-current-user-dropped persisted history length at this
        moment): for the conv-derived path the increment past the anchor is
        empty right after compaction; for the live-wire path the in-flight
        rounds are already folded into ``compacted_wire`` and any messages
        persisted into ``conv.messages`` AFTER this point (later rounds, the
        final assistant) become the verbatim ``rebuild(history[anchor:])``
        increment on the NEXT turn — no overlap, no duplication.

        Returns ``True`` when a NEW checkpoint was stored (so the follow-up
        loop knows to rebuild its growing ``wire_messages`` from the new base);
        ``False`` on no-op / failure (best-effort — never aborts a turn).
        ``conv.messages`` is left untouched.

        ``measured_eff_prompt`` (turn-internal trigger口径): the provider-
        measured ``实发`` size (``last_round_prompt_tokens`` +cache_read for
        Anthropic) of the MOST-RECENT completed round THIS turn. The non-force
        gate compares "what is actually being sent to the model" against
        ``0.8 × window``. Mid-turn, this round's usage has not yet landed on
        ``conv.messages`` (it is persisted only at turn end), so the loop
        threads its live ``last_round_usage`` in here. When non-``None`` it is
        used DIRECTLY as the ``实发`` reading; when ``None`` (presend / retry)
        the gate derives ``实发`` from the conv (last assistant's usage + this
        turn's new user message). Ignored when ``force`` is set.
        """
        if self._context_compressor is None:
            return False
        history = self._history_messages(conv=conv, request=request)
        if live_wire is None and len(history) <= _COMPRESS_PRESERVE_TAIL:
            return False

        assembled = (
            list(live_wire)
            if live_wire is not None
            else self._assemble_history_wire(conv=conv, request=request)
        )
        if len(assembled) <= _COMPRESS_PRESERVE_TAIL:
            return False

        model_id = request.model_hint or ""
        context_limit = get_context_limit(
            (model_id or "").removeprefix("local::") or "__unknown__"
        )

        # ── Conv-side inputs for the conversation-agnostic engine ──
        # Anchor for the differential attribution = the EXISTING checkpoint's
        # anchor (the messages already folded into the compacted head); the
        # increment past it is ``conv.messages[anchor:]``. Sliced here exactly
        # as the former ``_history_messages_since_anchor(conv, anchor)`` did
        # (over the FULL ``conv.messages`` — NOT the trailing-user-dropped
        # ``history`` — preserving the prior behaviour byte-for-byte) and
        # handed to the conv-free engine.
        _ckpt_for_attr = self._compaction_checkpoints.get(self._conv_key(conv))
        _anchor_for_attr = (
            _ckpt_for_attr.anchor_index if _ckpt_for_attr is not None else 0
        )
        _all_conv_msgs = list(getattr(conv, "messages", None) or [])
        history_since_anchor = (
            _all_conv_msgs[_anchor_for_attr:]
            if 0 <= _anchor_for_attr <= len(_all_conv_msgs)
            else _all_conv_msgs
        )

        # Anchor for the NEW checkpoint (口径 depends on live-wire vs history):
        #   * conv-derived path (``live_wire is None``): ``compacted_wire`` was
        #     built from ``_assemble_history_wire`` = the history WITHOUT the
        #     trailing current user, so the anchor is ``len(history)``.
        #   * live-wire path (mid-turn): ``compacted_wire`` was built from the
        #     loop's live wire (current user + every in-flight round = the FULL
        #     ``conv.messages``), so the anchor is ``len(conv.messages)`` — the
        #     current user must NOT be re-emitted as a cross-turn increment.
        anchor_index = (
            len(tuple(conv.messages)) if live_wire is not None else len(history)
        )
        # CCD-1 (PENDING-WORK.md §1): id of the LAST message inside the
        # compacted head (``conv.messages[anchor_index-1]``), so
        # ``estimate_compacted_tokens`` can pass it to
        # ``_last_assistant_with_usage(after_message_id=...)`` and ignore the
        # pre-compaction assistant's stale ``last_round_prompt_tokens``.
        # ``None`` when ``anchor_index == 0`` (the filter is a no-op).
        anchor_message_id: str | None = None
        if anchor_index > 0:
            try:
                _src_msgs = (
                    tuple(conv.messages) if live_wire is not None else history
                )
                if 0 <= anchor_index - 1 < len(_src_msgs):
                    _anchor_msg = _src_msgs[anchor_index - 1]
                    anchor_message_id = getattr(
                        getattr(_anchor_msg, "id", None), "value", None,
                    )
            except Exception:  # noqa: BLE001 — degrade to no anchor filter
                anchor_message_id = None

        # ``实发`` fallback for the non-force trigger gate: only needed when no
        # live measured eff_prompt is threaded (presend / retry). Computed
        # lazily so the conv-derived estimate is taken ONLY when the engine
        # would actually use it (byte-for-byte: the original computed it in the
        # same else-branch).
        presend_eff_fallback = 0
        if (
            not force
            and not (measured_eff_prompt is not None and measured_eff_prompt > 0)
        ):
            presend_eff_fallback = self._presend_eff_estimate(
                conv=conv, request=request, model_id=model_id
            )

        new_ckpt = await self._compaction_engine.maybe_compress(
            checkpoint_key=self._conv_key(conv),
            assembled_wire=assembled,
            history_messages_since_anchor=history_since_anchor,
            anchor_index=anchor_index,
            anchor_message_id=anchor_message_id,
            completed_rounds=(
                list(live_completed_rounds)
                if live_completed_rounds is not None
                else None
            ),
            model_hint=model_id,
            context_limit=context_limit,
            measured_eff_prompt=measured_eff_prompt,
            presend_eff_fallback=presend_eff_fallback,
            force=force,
            live_wire_mode=(live_wire is not None),
            persist_id=self._conv_id_for_key(conv),
        )
        if new_ckpt is None:
            return False
        # Operator hook: on truncate (context compression fired). Kept in the
        # use case (conv-aware hook engine) — the engine returns the new
        # checkpoint and the caller fires the conv-side side effects.
        await self._fire_hook(
            HookEvent.ON_TRUNCATE,
            payload={
                "original_count": len(assembled),
                "compressed_count": len(new_ckpt.compacted_wire),
            },
        )
        return True

    async def _maybe_presend_compress(
        self,
        *,
        conv: Any,
        request: "StreamChatInput",
        state: "_TurnBodyState | None" = None,
    ) -> "AsyncIterator[StreamFrame]":
        """P0-2: pre-send context compression (V1 chat_handler.py:419-450).

        Before opening the LLM stream, estimate the current context occupancy
        against the model's real context_length.  When usage exceeds the SOFT
        threshold (V1 ``0.80``), run compression proactively to avoid
        prompt-too-long rejections from cloud providers.

        ROOT FIX (compaction made effective): this now estimates + compresses
        the assembled OpenAI WIRE (含 role:tool outputs), the SAME口径 as the
        UI context badge — replacing the prior ``msg.content.text`` chars÷2.5
        heuristic that DROPPED every tool output and so under-counted by ~an
        order of magnitude (and never triggered on tool-heavy turns).  The
        result is stored as a session-level :class:`CompactionCheckpoint`
        (in-memory, per conversation), NOT written back onto ``conv.messages``
        (which stays the full original history) and NOT stashed for a single
        request — so the compaction is persistent + effective across turns and
        the steady state stops recompressing the whole history.

        Skips when no compressor is wired or the history is too small to
        compress (``<= COMPRESS_PRESERVE_TAIL``, checked inside
        ``_compress_via_checkpoint``).

        Progress frames (V2 enhancement): this is an **async generator**. It
        yields a :attr:`StreamFrameType.COMPACTION_PROGRESS` ``compressing``
        frame ONLY when the compaction step takes longer than
        :data:`_COMPACTION_PROGRESS_THRESHOLD_S` (≈2s — usually only when the
        Level 2 LLM summary runs), and a matching ``done`` frame when it
        finishes. Fast/no-op compactions (the common case) yield NOTHING, so the
        wire is byte-for-byte unchanged for the steady state. ``state`` (when
        supplied) sources the frame id / sequence counters so the synthesised
        frames slot into the turn's monotonic sequence; when ``None`` (tests
        calling this helper directly) the progress frames are skipped entirely.
        """
        # Defensive: ensure the per-turn stash never leaks a stale value into
        # the initial-stream path (the checkpoint, not the stash, now carries
        # compaction state).
        self._presend_compressed: tuple[Any, ...] | None = None  # type: ignore[attr-defined]
        if self._context_compressor is None:
            return
        if len(conv.messages) < 6:
            return

        compress_coro = self._compress_via_checkpoint(
            conv=conv,
            request=request,
            target_ratio=_COMPRESS_TARGET_RATIO,
        )

        # Without a ``state`` to mint frame ids (direct-call / test path), just
        # await the compaction — no progress frames.
        if state is None:
            await compress_coro
            return

        task = asyncio.ensure_future(compress_coro)
        progressing = False
        try:
            done, _pending = await asyncio.wait(
                {task}, timeout=_COMPACTION_PROGRESS_THRESHOLD_S
            )
            if not done:
                # Compaction is taking long enough for the user to notice —
                # surface a transient "compressing context…" indicator.
                progressing = True
                state.synth_seq += 1
                frame = StreamFrame.compaction_progress(
                    frame_id=f"compact-start-{state.synth_seq}",
                    sequence=state.synth_seq,
                    state="compressing",
                )
                # NB: per-frame ``ChatStreamFrameEvent`` are dropped at source
                # in ``_publish`` (the SSE/WS transports consume this async
                # iterator directly), so the frame reaches the client purely via
                # the ``yield`` below — no ``_publish`` call is needed.
                yield frame
                state.frame_count += 1
                # Now wait for the compaction to actually finish.
                await task
        finally:
            # Re-raise any compaction error exactly as the prior ``await`` did
            # (``_compress_via_checkpoint`` is best-effort and never raises, but
            # be defensive so a future change cannot swallow an exception).
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc is not None and not progressing:
                    raise exc
        # Clear the indicator (only when we showed one).
        if progressing:
            state.synth_seq += 1
            done_frame = StreamFrame.compaction_progress(
                frame_id=f"compact-done-{state.synth_seq}",
                sequence=state.synth_seq,
                state="done",
            )
            yield done_frame
            state.frame_count += 1

    async def _build_synthetic_retry_history(
        self,
        *,
        conv: Any,
        compressed_history: tuple[Any, ...] | None,
        synthetic_user_text: str,
    ) -> tuple[Any, ...]:
        """Append a synthetic user nudge to the current history.

        Thin wrapper over
        :func:`_streaming_helpers.build_synthetic_retry_history` (ARCH-1 /
        A-3 cohesion split): the body never awaited / used ``self``; the
        method stays ``async`` so the awaiting call site is unchanged.
        Logic byte-for-byte unchanged.
        """
        return _build_synthetic_retry_history_fn(
            conv=conv,
            compressed_history=compressed_history,
            synthetic_user_text=synthetic_user_text,
        )

    def _schedule_experience_extraction(
        self,
        *,
        round_no: int,
        last_tool_round: int,
        conv: Any,
        tab: Any,
    ) -> None:
        """Fire-and-forget LLM-powered experience extraction (PR-091 H-6).

        Skips silently when:

        * the extractor is not wired (``self._experience_extractor is None``);
        * the trigger gate fails (need ``round_no > 2`` AND a tool call
          actually ran in this turn — i.e. ``last_tool_round >= 1``);
        * scheduling itself raises (no event loop available, etc.).
        """
        if self._experience_extractor is None:
            return
        try:
            should_run = bool(
                self._experience_extractor.should_extract(
                    round_index=round_no,
                    tool_call_count=last_tool_round,
                ),
            )
        except Exception:  # noqa: BLE001 — defensive
            return
        if not should_run:
            return

        # Snapshot the conversation messages for the background task so
        # subsequent mutations to ``conv`` don't affect the extraction.
        messages_snapshot = list(conv.messages)
        try:
            asyncio.create_task(
                self._experience_extractor.extract(
                    messages_snapshot,
                    conversation_id=conv.id,
                    tab_id=tab.id,
                ),
            )
        except RuntimeError:  # no running event loop — best-effort, skip
            _log.debug("chat.experience_extraction_no_loop")

    async def _execute_single_tool_call(
        self,
        *,
        tc_frame: StreamFrame,
        tab: Any,
        conv: Any,
        guardrail: GuardrailPort,
        request: "StreamChatInput",
        tool_results: list[dict[str, Any]],
        seq: int,
        precomputed_hook_record: "HookFiredRecord | None" = None,
        hook_already_fired: bool = False,
    ) -> StreamFrame | None:
        """Run one non-agent tool call; mutate ``tool_results``; return frame.

        Single-tool slice of :meth:`_run_followup_loop` (B1 cohesion
        split).  Behaviour byte-for-byte identical to the inline body:

        a. ``GuardrailPort.check`` — ``BLOCK`` short-circuits with a
           synthetic ``[guardrail_blocked] {reason}`` result;
        b. otherwise ``ToolInvocationPort.invoke`` (``[tool_error] {exc}``
           on exception);
        c. ``GuardrailPort.observe`` with the success flag;
        d. truncate via ``ToolResultTruncatorPort``;
        e. append the result dict to ``tool_results`` (mutated in place);
        f. return the synthetic ``TOOL_RESULT`` frame stamped at ``seq``.

        Returns ``None`` (and does not touch ``tool_results``) when the
        frame carries no usable ``tool_name`` — the caller then skips
        without advancing ``seq`` (parity with the old ``continue``).

        ``hook_already_fired`` / ``precomputed_hook_record``: when the
        streaming caller already fired ``PRE_TOOL_CALL`` (to decide routing)
        and falls back here, it passes ``hook_already_fired=True`` plus the
        record it obtained, so this method REUSES that record's interceptor
        directives instead of firing the hook a SECOND time (avoids a double
        subprocess spawn / double audit side effect on the deny path). Default
        ``hook_already_fired=False`` fires the hook here as before (the
        non-streaming direct-entry path is unchanged).
        """
        tool_name = tc_frame.payload.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            return None
        arguments = tc_frame.payload.get("arguments", {}) or {}
        if not isinstance(arguments, dict):
            arguments = {}

        # Operator hook: pre tool call (before the guardrail check). An
        # interceptor hook may steer the call via its stdout JSON directive:
        #   * decision=deny/ask → block the call, synthesise a
        #     ``[hook_blocked] {reason}`` result fed back to the model;
        #   * updated_input      → replace the model's arguments (e.g. a hook
        #     that normalises a path or injects a mandatory flag) BEFORE the
        #     guardrail check + invoke see them.
        # A plain logging hook (no JSON) returns no directive → unchanged.
        # When the streaming caller already fired the hook (to decide routing)
        # and fell back here, REUSE that record instead of firing again.
        if hook_already_fired:
            _pre_rec = precomputed_hook_record
        else:
            _pre_rec = await self._fire_hook(
                HookEvent.PRE_TOOL_CALL,
                payload={"tool_name": tool_name, "args": dict(arguments)},
            )
        _hook_denied = False
        _hook_deny_reason = ""
        if _pre_rec is not None:
            if _pre_rec.decision in (HookDecision.DENY, HookDecision.ASK):
                _hook_denied = True
                _hook_deny_reason = (
                    _pre_rec.reason
                    or f"tool {tool_name!r} blocked by pre_tool_call hook"
                )
            elif _pre_rec.updated_input is not None:
                arguments = dict(_pre_rec.updated_input)

        verdict = guardrail.check(tool_name=tool_name, arguments=arguments)
        # Wall-clock timing around the actual tool work (guardrail check +
        # invoke). Stamped onto the final TOOL_RESULT frame as ``duration_ms``
        # so the UI shows the tool's run time in both live AND history modes
        # (V2 enhancement; V1 only timed the live indicator, never persisted).
        _tool_started_ms = _now_ms(self._clock)
        if _hook_denied:
            # Interceptor hook vetoed this call — do NOT execute the tool.
            # Surfaced as a distinct ``[hook_blocked]`` result (not a guardrail
            # block / error) so the LLM understands the call was refused by an
            # operator hook and can adapt.
            raw_result: Any = f"[hook_blocked] {_hook_deny_reason}"
            success = False
        elif tool_name in (
            _session_disabled_tools(request.extra)
            | _persona_disabled_tools(request.extra)
        ):
            # Per-session + per-persona EXECUTION gate. The user switched
            # this tool OFF for this conversation via the
            # SessionToolsPopover, or the active coding persona does not
            # permit this tool group. The advertise filter
            # (``_collect_tool_schemas``) already hides it from the model, but
            # this gate is the AUTHORITATIVE, defensive backstop: even if the
            # model calls a non-advertised tool (from history / habit), it is
            # NOT executed. Surfaced as a distinct ``[tool_disabled]`` result
            # (not a guardrail block / error) so the LLM understands the tool
            # is unavailable this turn. Applied per-turn only — never mutates
            # global tool-safety config.
            raw_result = (
                f"[tool_disabled] tool {tool_name!r} is turned off for this "
                f"conversation (per-session setting or persona restriction)"
            )
            success = False
        elif verdict.decision is GuardrailDecision.BLOCK:
            raw_result = f"[guardrail_blocked] {verdict.reason}"
            success = False
        else:
            try:
                invocation = await self._tools.invoke(
                    ToolInvocationRequest(
                        tab_id=tab.id,
                        conversation_id=conv.id,
                        tool_name=tool_name,
                        arguments=dict(arguments),
                    ),
                )
                if isinstance(invocation, ToolInvocationResult):
                    raw_result = invocation.result
                    success = invocation.ok
                else:  # adapter returned a bare value
                    raw_result = invocation
                    success = True
            except Exception as exc:  # pragma: no cover - defensive
                raw_result = f"[tool_error] {exc}"
                success = False

        guardrail.observe(
            tool_name=tool_name,
            arguments=arguments,
            result=raw_result,
            success=success,
        )

        # Operator hook: post tool call (after observe; payload mirrors
        # the legacy harness POST_TOOL_CALL fields).
        await self._fire_hook(
            HookEvent.POST_TOOL_CALL,
            payload={
                "tool_name": tool_name,
                "args": dict(arguments),
                "ok": success,
            },
        )

        # Truncate before feeding back to the LLM.
        result_text = (
            raw_result if isinstance(raw_result, str) else str(raw_result)
        )
        trunc = self._tool_result_truncator.truncate(
            ToolResultTruncationRequest(
                model_id=request.model_hint or "",
                tool_name=tool_name,
                result_text=result_text,
                # Skip the head+tail split when the tool already bounded its
                # own output (self-reported ``truncated`` / persisted
                # ``stored_path``) so its recovery footer is never corrupted.
                already_truncated=_kernel_tool_already_truncated(raw_result),
            ),
        )
        tool_results.append(
            {
                "tool_name": tool_name,
                "tool_call_id": tc_frame.payload.get("tool_call_id"),
                "arguments": dict(arguments),
                "result": trunc.text,
                "ok": success,
                "truncated": trunc.truncated,
                # PR-090 C-1: carry the Vertex AI thought_signature (if the
                # originating frame had one) so ``_append_tool_round`` can
                # re-attach it to the rebuilt assistant.tool_calls[i].
                "thought_signature": tc_frame.payload.get("thought_signature"),
            },
        )
        return StreamFrame.tool_result(
            frame_id=f"fu-tr-{seq}",
            sequence=seq,
            tool_name=tool_name,
            result=trunc.text,
            # Appended fields (AGENTS.md §3.1): surface the *original*
            # output size + truncation flag so the chat UI can render
            # the size badge + "已截断" warning (V1
            # ToolExecPanel.js:148-189). ``result`` still carries the
            # head+tail summary as before.
            size=trunc.original_length,
            truncated=trunc.truncated,
            # Tool wall-clock run time (ms) — see StreamFrame.tool_result.
            duration_ms=max(0, _now_ms(self._clock) - _tool_started_ms),
            # Pair-by-id key (see streaming final-frame note): bind result
            # to its originating call by id for position-independent
            # persistence pairing.
            tool_call_id=tc_frame.payload.get("tool_call_id"),
        )

    async def _execute_tool_call_streaming(
        self,
        *,
        tc_frame: StreamFrame,
        tab: Any,
        conv: Any,
        guardrail: GuardrailPort,
        request: "StreamChatInput",
        tool_results: list[dict[str, Any]],
        seq: int,
    ) -> AsyncIterator[StreamFrame]:
        """Run one non-agent tool call, streaming partial frames for exec.

        WIRE-tools (V1 ``backend/tools/_exec.py:1010`` + ``useChat.js:1041``
        parity): probe the optional :meth:`ToolInvocationPort.invoke_streaming`
        capability.  When the tool is streaming-capable (today: ``exec``)
        the adapter returns an :class:`AsyncIterator` of
        :class:`ToolStreamChunk`; we forward each STDOUT / STDERR chunk as a
        ``partial=True`` ``tool_result`` frame (with the chunk text in both
        ``delta`` and ``result`` so consumers ignoring ``partial`` still see
        content) and turn the terminal ``DONE`` chunk into the final
        ``partial=False`` frame — exactly the same head+tail-truncated
        result that the one-shot path produces, so the LLM-facing
        ``tool_results`` entry and ``size`` / ``truncated`` metadata are
        byte-for-byte identical to the pre-WIRE behaviour.

        Non-streaming tools (``invoke_streaming`` absent or returns
        ``None``) fall through to the unchanged one-shot
        :meth:`_execute_single_tool_call` — they emit a single final frame
        and zero partials (byte-for-byte prior behaviour).

        Yields zero frames (and touches nothing) when the frame carries no
        usable ``tool_name`` — the caller then skips without advancing
        ``seq`` (parity with the old ``continue``).
        """
        tool_name = tc_frame.payload.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            return
        arguments = tc_frame.payload.get("arguments", {}) or {}
        if not isinstance(arguments, dict):
            arguments = {}

        # Operator hook: pre tool call. MUST fire BEFORE the streaming
        # capability probe / guardrail check / ``stream_factory`` call below,
        # so an interceptor hook can veto (decision=deny/ask) or rewrite
        # (updated_input) the call before the streamed tool actually starts.
        # A ``deny`` routes to the one-shot fallback (``chunk_iter=None``),
        # where ``_execute_single_tool_call`` re-fires the hook and emits the
        # ``[hook_blocked]`` result WITHOUT executing — so the veto is honoured
        # authoritatively there. ``updated_input`` replaces the arguments the
        # streaming invocation sees. A plain logging hook (no directive) is
        # unchanged.
        _pre_rec = await self._fire_hook(
            HookEvent.PRE_TOOL_CALL,
            payload={"tool_name": tool_name, "args": dict(arguments)},
        )
        _hook_denied_stream = False
        if _pre_rec is not None:
            if _pre_rec.decision in (HookDecision.DENY, HookDecision.ASK):
                _hook_denied_stream = True
            elif _pre_rec.updated_input is not None:
                arguments = dict(_pre_rec.updated_input)

        # Probe the optional streaming capability.  Absent / non-streaming
        # tools → one-shot fallback (unchanged behaviour).
        stream_factory = getattr(self._tools, "invoke_streaming", None)
        chunk_iter: AsyncIterator[ToolStreamChunk] | None = None
        # Per-session execution gate: a tool the user turned OFF for this
        # conversation must NOT stream (it would execute before the one-shot
        # gate runs). Forcing ``chunk_iter = None`` routes it through the
        # one-shot fallback below, where ``_execute_single_tool_call`` returns
        # the ``[tool_disabled]`` result without invoking the tool. A hook veto
        # (``_hook_denied_stream``) likewise routes to the one-shot path so the
        # ``[hook_blocked]`` result is emitted there without streaming.
        _disabled_this_turn = tool_name in (
            _session_disabled_tools(request.extra)
            | _persona_disabled_tools(request.extra)
        )
        if (
            stream_factory is not None
            and not _disabled_this_turn
            and not _hook_denied_stream
        ):
            # The guardrail BLOCK short-circuit must stay identical to the
            # one-shot path, so only stream when the guardrail allows.
            verdict = guardrail.check(tool_name=tool_name, arguments=arguments)
            if verdict.decision is not GuardrailDecision.BLOCK:
                try:
                    chunk_iter = stream_factory(
                        ToolInvocationRequest(
                            tab_id=tab.id,
                            conversation_id=conv.id,
                            tool_name=tool_name,
                            arguments=dict(arguments),
                        ),
                    )
                except Exception:  # noqa: BLE001 — defensive: fall back
                    chunk_iter = None

        if chunk_iter is None:
            # One-shot fallback path (non-streaming tool / opted out /
            # guardrail BLOCK / hook veto).  Reuse the byte-for-byte building
            # block — pass the ALREADY-fired hook record so it applies the same
            # deny/updated_input interceptor semantics WITHOUT firing the hook a
            # second time (no double subprocess spawn / double audit side-effect).
            tr_frame = await self._execute_single_tool_call(
                tc_frame=tc_frame,
                tab=tab,
                conv=conv,
                guardrail=guardrail,
                request=request,
                tool_results=tool_results,
                seq=seq,
                precomputed_hook_record=_pre_rec,
                hook_already_fired=True,
            )
            if tr_frame is not None:
                yield tr_frame
            return

        # --- Streaming path -------------------------------------------------
        # (PRE_TOOL_CALL already fired at the top of this method — before the
        # streaming capability probe — so the interceptor could veto / rewrite
        # the call before the streamed tool started. Do NOT re-fire here.)

        local_seq = seq
        done_chunk: ToolStreamChunk | None = None
        raw_result: Any = ""
        success = True
        stream_failed = False
        # Wall-clock timing around the streamed tool work — stamped on the
        # FINAL frame as ``duration_ms`` (V2 enhancement; see one-shot path).
        _tool_started_ms = _now_ms(self._clock)
        try:
            # ORPHAN-KILL FIX: guard the exec stream generator with
            # ``aclosing`` so that when THIS generator is abandoned mid-stream
            # — the parallel-tool driver / single-tool fast path does a bare
            # ``return`` on a user Stop (see ``_execute_and_stream_other_tool
            # _calls`` ``if handle.is_set(): return``), which throws
            # ``GeneratorExit`` into us at the ``yield`` below — the INNER
            # ``chunk_iter`` (``di.py::_exec_stream`` → ``stream_exec``, which
            # OWNS the live subprocess) is DETERMINISTICALLY ``aclose()``-d.
            # Without this, a bare ``async for`` leaves ``chunk_iter``
            # un-closed on abandonment: its ``finally`` (and ``stream_exec``'s
            # child tree-kill) then only fire on non-deterministic GC / the
            # loop's async-gen finalizer, so the ``python`` child kept running
            # to 39 s / 129 s AND no terminal DONE chunk was produced — the
            # tool card stayed "执行中" with a live Stop button even after the
            # tab was closed and reopened (state persisted, never settled).
            # ``aclosing`` makes the teardown synchronous with the abort so the
            # child is killed and the round unwinds promptly.
            async with contextlib.aclosing(chunk_iter) as _guarded_iter:
                async for chunk in _guarded_iter:
                    if chunk.kind is ToolStreamChunkKind.DONE:
                        done_chunk = chunk
                        break
                    # STDOUT / STDERR increment → partial frame.
                    if not chunk.text:
                        continue
                    yield StreamFrame.tool_result(
                        frame_id=f"fu-tr-{local_seq}",
                        sequence=local_seq,
                        tool_name=tool_name,
                        # Mirror text into ``result`` so consumers ignoring the
                        # ``partial`` flag still observe streamed content.
                        result=chunk.text,
                        partial=True,
                        delta=chunk.text,
                        # Carry the call id on partials too so the UI accumulates
                        # the live stream onto the correct card when parallel
                        # same-named tools (e.g. two exec) stream concurrently.
                        tool_call_id=tc_frame.payload.get("tool_call_id"),
                    )
                    local_seq += 1
        except Exception as exc:  # noqa: BLE001 — surface as a failed result
            raw_result = f"[tool_error] {exc}"
            success = False
            stream_failed = True

        if done_chunk is not None:
            raw_result = done_chunk.result if done_chunk.result is not None else ""
            success = done_chunk.ok
        elif not stream_failed:
            # The streaming iterator ended WITHOUT yielding a terminal DONE
            # chunk (and without raising — a raise is handled above as a failed
            # result). Per the ``ToolInvocationPort.invoke_streaming`` contract
            # (ports.py: "exactly one terminal DONE chunk") a missing DONE means
            # the tool did NOT actually complete — the stream was cut short
            # (a non-conforming adapter, a generator that returned early, or an
            # upstream that closed mid-run). 🔴 State-Truth-First (铁律 1): the
            # truth of "tool finished" is a real DONE chunk, NOT "the iterator
            # happened to stop". We must NOT fabricate an empty SUCCESS here:
            # that feeds the LLM a fake completed result, so it narrates the
            # next round + issues new tool calls while the real work never
            # finished (the reported "exec card still spinning while the model
            # already moved on" class of bug). Surface it as a FAILED result
            # with an explicit marker so the model sees the tool did not
            # succeed instead of being misled into continuing.
            raw_result = "[tool_error] tool stream ended without a result"
            success = False


        guardrail.observe(
            tool_name=tool_name,
            arguments=arguments,
            result=raw_result,
            success=success,
        )
        await self._fire_hook(
            HookEvent.POST_TOOL_CALL,
            payload={
                "tool_name": tool_name,
                "args": dict(arguments),
                "ok": success,
            },
        )

        # Final frame: truncate exactly like the one-shot path so the
        # LLM-facing ``tool_results`` entry + size/truncated badge match.
        result_text = (
            raw_result if isinstance(raw_result, str) else str(raw_result)
        )
        trunc = self._tool_result_truncator.truncate(
            ToolResultTruncationRequest(
                model_id=request.model_hint or "",
                tool_name=tool_name,
                result_text=result_text,
                # Skip the head+tail split when the tool already bounded its
                # own output (self-reported ``truncated`` / persisted
                # ``stored_path``) so its recovery footer is never corrupted.
                already_truncated=_kernel_tool_already_truncated(raw_result),
            ),
        )
        tool_results.append(
            {
                "tool_name": tool_name,
                "tool_call_id": tc_frame.payload.get("tool_call_id"),
                "arguments": dict(arguments),
                "result": trunc.text,
                "ok": success,
                "truncated": trunc.truncated,
                # PR-090 C-1: carry the Vertex AI thought_signature (if the
                # originating frame had one) so ``_append_tool_round`` can
                # re-attach it to the rebuilt assistant.tool_calls[i].
                "thought_signature": tc_frame.payload.get("thought_signature"),
            },
        )
        yield StreamFrame.tool_result(
            frame_id=f"fu-tr-{local_seq}",
            sequence=local_seq,
            tool_name=tool_name,
            result=trunc.text,
            size=trunc.original_length,
            truncated=trunc.truncated,
            # Explicit terminal marker so consumers can distinguish the
            # final frame from the partials above (also matches V1's
            # ``{type:"done"}`` after ``{type:"output"}`` frames).
            partial=False,
            # Pair-by-id key: carry the originating tool_call's id back on
            # the final result so persistence pairs result→call by id, not
            # by positional index (a streaming exec tool emits several
            # partial frames first, which would shift positional pairing).
            tool_call_id=tc_frame.payload.get("tool_call_id"),
            # Tool wall-clock run time (ms) — see StreamFrame.tool_result.
            duration_ms=max(0, _now_ms(self._clock) - _tool_started_ms),
        )

    async def _run_followup_loop(
        self,
        *,
        tab: Any,
        conv: Any,
        initial_tool_call: StreamFrame,
        handle: StreamAbortHandle,
        assistant_text_parts: list[str],
        request: "StreamChatInput",
        seq_start: int,
        extra_initial_tool_calls: list[StreamFrame] | None = None,
        initial_usage: dict[str, Any] | None = None,
        round_request_ids: dict[int, str] | None = None,
        last_round_usage_out: dict[str, Any] | None = None,
        injected_messages_out: list[tuple[int, Message]] | None = None,
    ) -> AsyncIterator[StreamFrame]:
        """Execute the tool(s) inline and run follow-up rounds via the kernel.

        Mirrors the legacy ``backend/chat_handler.py:696-860`` follow-up loop,
        context-isolated. Now driven by the SHARED
        :class:`SingleAgentTurnKernel` (§15): the kernel owns the round
        skeleton (abort check → inter-round compaction → build send wire →
        open stream → drain & classify → execute tools → grow wire → cap) and
        this method injects the main loop's专有 machinery via the kernel's
        optional hooks so the outbound wire + frames stay byte-for-byte
        identical:

        * round 0's batched initial tool calls are dispatched HERE (before the
          kernel) and their block appended to the wire — so kernel round ``k``
          maps 1:1 to the main loop's ``round_no=k`` follow-up LLM stream
          (whose calls execute in that same kernel round, results bound to
          round ``k``, exactly as the old dispatch-at-top-of-``k+1`` bound them
          to the issuing round ``k``);
        * ``compact_hook`` = the dual-track checkpoint + completed-rounds
          replay (returns the rebuilt wire for the kernel to swap in);
        * ``on_round_open`` = per-round prompt snapshot (returns the round's
          ``request_id``, stashed for the emitter);
        * ``open_round_stream`` = ``_build_llm_request`` + ``self._llm.stream``;
        * ``tool_executor`` = ``_dispatch_pending_tool_calls`` (agent recursion
          streaming + exec partials) re-shaped as a partial/final producer;
        * ``grow_wire_hook`` = ``_append_tool_round`` + ``completed_rounds``
          bookkeeping (the main loop owns wire growth so id precedence /
          thought_signature / ``content:None`` vs sentinel match exactly);
        * ``on_round_end`` = usage accumulation + empty-completion retry +
          experience extraction + no-progress breaker.

        The emitter adapts each :class:`KernelEvent` back into a
        :class:`StreamFrame` stamped with its round + ``request_id``.

        Purely a producer: the caller integrates each yielded frame into the
        outer turn (publish + capture chunk text + watch abort).

        Per-conversation token budget (``max_budget_tokens``)
        -----------------------------------------------------
        When a :class:`BudgetTrackerPort` is wired AND the conversation has a
        cap, each completed round's ``_on_round_end`` folds that round's
        PROVIDER-MEASURED net-new tokens (``effective_prompt_tokens`` +
        ``completion_tokens`` — the single-round ``last_round_*`` figure, never
        the Anthropic-corrupt cumulative SUM) into the shared pool via
        ``observe`` and re-``check``s it. DECISION — *round-start enforcement +
        allow-overshoot-then-stop*: the round that pushes usage over the cap has
        ALREADY been sent (we cannot un-send it), so we let it FINISH but do NOT
        open the next round; the emitter closes the turn with
        ``END(reason="budget_exceeded")``. This is honest and predictable (the
        user sees exactly one over-budget round, then a clear stop) — the
        alternative (blocking a round we already committed to) is impossible,
        and pre-emptively refusing a round whose size we cannot predict would be
        arbitrary. Round 0 always fires (dispatched before this loop); its usage
        is observed on the first ``_on_round_end``, so a conversation already at
        the cap stops right after round 0 rather than before it (same
        allow-overshoot rule). Local (on-device) turns are SKIPPED entirely (NPU
        emits no authoritative usage — never counted, never blocked; a
        once-per-turn advisory chunk explains this), and a round reporting
        prompt=0 AND completion=0 (Gemini partial reliability) is not enforced
        (State-Truth-First: no truth to enforce against). ``None`` tracker /
        no cap ⇒ byte-for-byte unchanged behaviour.
        """
        assert self._guardrail_factory is not None
        assert self._tool_result_truncator is not None

        guardrail = self._guardrail_factory()
        seq = seq_start
        pending_tool_calls: list[StreamFrame] = [initial_tool_call]
        # ── V1 chat_handler.py:576-587 parity: a single LLM turn may emit
        # multiple TOOL_CALL frames (cloud Claude's batched tool_use
        # response).  ``extra_initial_tool_calls`` carries the rest from
        # the same turn so they all fan out in parallel within the first
        # follow-up round.
        if extra_initial_tool_calls:
            pending_tool_calls.extend(extra_initial_tool_calls)
        tool_results: list[dict[str, Any]] = []
        compressed_history_override: tuple[Any, ...] | None = None
        compaction_token: Any = object()
        empty_completion_retry_used = False
        last_tool_round = 0

        # ── V1 token-usage accumulation (useChat.js:2351-2356/2411) ──────
        usage_accumulator: dict[str, int] = {}
        self._accumulate_usage(usage_accumulator, initial_usage)
        # Running full-history counter keystone: the LAST round's usage (the
        # true wire size of this turn). ``usage_accumulator`` SUMs prompt_tokens
        # across rounds, so it is NOT the wire size for multi-round turns. Seed
        # with round-0's initial usage so single-round turns (loop runs 0
        # follow-up rounds before terminal END) still carry it; each subsequent
        # round overwrites it in ``_on_round_end``. Surfaced to the caller via
        # the ``last_round_usage_out`` holder (caller copies to
        # ``state.last_round_usage``).
        last_round_usage: dict[str, Any] | None = (
            initial_usage if isinstance(initial_usage, dict) else None
        )

        # ── Per-conversation token-budget (max_budget_tokens) turn state ──────
        # ``_budget_exceeded`` flips True once a completed round pushes the
        # conversation's cumulative used-tokens at/over the cap; the emitter
        # then closes the turn with ``END(reason="budget_exceeded")`` instead of
        # starting another round (DECISION: allow the round that would overshoot
        # to FINISH — we can't un-send it — but do NOT open the next; honest,
        # predictable UX). ``_budget_local_hinted`` throttles the "budget not
        # enforceable on local turns" advisory to once per turn;
        # ``_budget_local_hint_pending`` is set by ``_on_round_end`` and drained
        # (emitted as a marked chunk) by the emitter — kept OUT of
        # ``_on_round_end`` because that hook returns a value (not an async
        # generator; it must not ``yield``). Declared HERE (before the round-0
        # fold below) so the round-0 short-circuit can set ``_budget_exceeded``.
        _budget_exceeded: bool = False
        # Captured when the cap is hit so the terminal END can carry the
        # decision metadata (used / cap / next-cap) for the frontend dialog.
        _budget_used_at_stop: int = 0
        _budget_max_at_stop: int = 0
        _budget_local_hinted: bool = False
        _budget_local_hint_pending: bool = False

        # ── Per-conversation budget: fold ROUND 0's usage once (max_budget) ──
        # Round 0's LLM stream is drained in the outer ``_run`` (before this
        # loop), so its usage never reaches ``_on_round_end`` (which only fires
        # for follow-up rounds). Fold it here ONCE so the shared pool reflects
        # round 0 too. Skip local turns / no-measurement rounds (State-Truth-
        # First). A subsequent ``check`` here also short-circuits the very first
        # round for a conversation ALREADY over the cap from a PRIOR turn:
        # nothing new was sent yet this loop, so we honestly stop before opening
        # round 1 (round 0 already fired — allow-overshoot). Best-effort.
        #
        # ``enabled`` GATE (parity with the follow-up round path at
        # ``_on_round_end`` — ``if _br.enabled``): only touch the tracker when a
        # cap is actually configured for this conversation. Without the gate,
        # EVERY turn of EVERY conversation (cap set or not) would ``observe`` →
        # find+save the conversation header once, an unnecessary per-turn write
        # for the common (no-budget) case now that ``chat_budget_enabled``
        # defaults to True. ``check`` is a cheap read; ``observe`` (the write) is
        # skipped when no cap is set.
        if not _is_local_model_hint(request.model_hint) and isinstance(
            initial_usage, dict
        ):
            _r0_pt = int(initial_usage.get("prompt_tokens") or 0)
            _r0_comp = int(initial_usage.get("completion_tokens") or 0)
            if not (_r0_pt == 0 and _r0_comp == 0):
                _r0_check = await self._budget.check(conv.id)
                if _r0_check.enabled:
                    _r0_eff = _effective_prompt_tokens(
                        initial_usage,
                        is_anthropic=_is_anthropic_family(request.model_hint or ""),
                        include_cache_write_fallback=True,
                    )
                    _r0_delta = _r0_eff + _r0_comp
                    if _r0_delta > 0:
                        await self._budget.observe(conv.id, _r0_delta)
                        _r0_check = await self._budget.check(conv.id)
                    if _r0_check.exceeded:
                        _budget_exceeded = True
                        _budget_used_at_stop = int(_r0_check.used or 0)
                        _budget_max_at_stop = int(_r0_check.max_tokens or 0)

        # ── V1 ``full_messages`` incremental model ───────────────────────
        wire_messages: list[dict[str, Any]] = self._build_base_wire_messages(
            conv=conv,
            request=request,
            compressed_history=compressed_history_override,
        )
        completed_rounds: list[_CompletedRound] = []
        # The tool calls + lead-in text for the round about to be dispatched.
        current_round_calls: list[StreamFrame] = list(pending_tool_calls)
        current_assistant_text: str = "".join(assistant_text_parts).strip()

        # No-progress circuit breaker state (Fix B).
        _last_call_sig: str | None = None
        _repeat_count = 0

        # Shared-prefix snapshot turn segment (O(N) not O(N²)).
        _snapshot_turn_ref: str = str(uuid.uuid4())

        # ── Turn-internal context-usage live refresh (V2 enhancement) ─────
        # The main-conversation context badge previously refreshed only at the
        # turn boundary (frontend re-fetch of ``GET /context``); during a long
        # multi-round tool turn the real wire grows (e.g. 33K → 70K) but the
        # badge stayed frozen at the prior turn's value. ``_context_usage_frame``
        # builds a ``context_usage`` frame from the MOST-RECENT completed round's
        # PROVIDER-MEASURED ``real_prompt_tokens`` (State-Truth-First 铁律 1 —
        # the real wire size the model saw, NOT an estimate) so the badge can
        # refresh per round WHILE the turn runs. Returns ``None`` (emit NOTHING)
        # when there is no completed round yet OR the round produced no measured
        # prompt size (``real_prompt_tokens <= 0`` — local model / no usage): we
        # never push an estimate (no regression; the badge keeps its last
        # authoritative value). The mirror of the sub-agent per-round
        # ``used_tokens`` refresh. The turn-boundary ``GET /context`` remains the
        # authoritative override (铁律 3: optimistic feedback + probe override).
        def _context_usage_frame(*, seq: int) -> "StreamFrame | None":
            if not completed_rounds:
                return None
            # ── EFF口径对齐 turn 边界 GET /context（避免 badge 跳变）─────────
            # The turn-boundary authoritative badge (``CompactChatUseCase`` via
            # ``conv.full_history_tokens``) measures the wire with the EFF
            # formula: ``prompt_tokens + cache_read_tokens`` for Anthropic /
            # Claude (which split cache reads OUT of ``prompt_tokens``), plain
            # ``prompt_tokens`` otherwise. ``_CompletedRound.real_prompt_tokens``
            # is the RAW ``prompt_tokens`` (no cache_read add-back) because the
            # differential compressor needs that口径 — we must NOT change it.
            # If this live frame used the raw value, a heavily-cached Claude turn
            # would show a SMALLER live ``used`` mid-turn and then JUMP UP at the
            # turn boundary when ``/context`` reports the eff value. So compute
            # the eff figure HERE from the round's RAW usage dict (nonlocal
            # ``last_round_usage``, keys ``prompt_tokens`` / ``cache_read_tokens``
            # — NOT the ``last_round_*`` tail keys) via the SHARED
            # ``effective_prompt_tokens`` helper, keeping the live badge and the
            # boundary badge on ONE口径. NO cache_write fallback here (point ④
            # keeps the raw eff口径 — same as the autonomous ① path; the
            # cache_write add-back belongs only to the accounting paths ②③). The
            # helper returns 0 for a missing / malformed usage dict OR a
            # non-positive eff, so the single ``eff if eff > 0 else _raw_real``
            # fallback covers BOTH the no-usage-dict and zero-eff cases
            # (byte-for-byte equivalent to the prior if/else) — falling back to
            # the raw cumulative ``real_prompt_tokens`` (local / no-measurement —
            # better than emitting nothing).
            _raw_real = completed_rounds[-1].real_prompt_tokens
            _eff = _effective_prompt_tokens(
                last_round_usage if isinstance(last_round_usage, dict) else None,
                is_anthropic=_is_anthropic_family(request.model_hint),
                include_cache_write_fallback=False,
            )
            _used: int = _eff if _eff > 0 else (
                _raw_real if isinstance(_raw_real, int) else 0
            )
            if not isinstance(_used, int) or _used <= 0:
                return None
            _limit = get_context_limit(
                (request.model_hint or "").removeprefix("local::")
                or "__unknown__"
            )
            if not isinstance(_limit, int) or _limit <= 0:
                return None
            return StreamFrame.context_usage(
                frame_id=f"fu-ctx-{seq}",
                sequence=seq,
                used_tokens=_used,
                context_limit=_limit,
            )

        # ── round-0 dispatch (the initial turn's tool calls) ──────────────
        # The main loop historically dispatched round 0's calls at the TOP of
        # its first iteration; with the kernel driving "stream-then-execute"
        # rounds we dispatch them HERE so kernel round 1 == the first follow-up
        # LLM stream. Their TOOL_RESULT frames are bound to round 0 (the
        # issuing round) and round 0's request_id (saved by
        # ``_open_initial_stream`` before the first frame) — identical to the
        # old behaviour. A compaction never fires on round 0 (gate: round>1).
        dispatch_outcome = _FollowupRoundOutcome(seq=seq)
        results_before = len(tool_results)
        async for fr in self._dispatch_pending_tool_calls(
            pending_tool_calls=pending_tool_calls,
            guardrail=guardrail,
            tab=tab,
            conv=conv,
            request=request,
            tool_results=tool_results,
            handle=handle,
            outcome=dispatch_outcome,
            round_index=0,
        ):
            _result_rid = (
                round_request_ids.get(0)
                if round_request_ids is not None
                else None
            )
            yield _stamp_request_id(_stamp_round(fr, 0), _result_rid)
        seq = dispatch_outcome.seq
        if dispatch_outcome.aborted:
            return
        last_tool_round = 1
        pending_tool_calls = []

        # Record round 0's executed block + grow the wire.
        round0_results = tool_results[results_before:]
        if round0_results:
            # Round 0's ``real_prompt_tokens`` = the size of the wire it
            # actually saw (system + tools + user), which IS the prompt size
            # ``initial_usage`` reports for round 0's stream. Tokenizer
            # fallback when no cloud usage is available (e.g. local models or
            # empty usage block). State-Truth-First (AGENTS.md 铁律 1): cloud
            # measurement is preferred; tokenizer only runs on the wire's text
            # content (already a bounded one-shot pass at turn start, not a
            # per-history full re-tokenisation).
            _r0_real, _r0_comp, _r0_src = self._real_prompt_for_initial_round(
                usage=initial_usage,
                wire=wire_messages,
                model_hint=request.model_hint,
            )
            completed_rounds.append(
                _CompletedRound(
                    text=current_assistant_text,
                    tool_calls=list(current_round_calls),
                    tool_results=list(round0_results),
                    real_prompt_tokens=_r0_real,
                    completion_tokens=_r0_comp,
                    source=_r0_src,
                )
            )
            self._append_tool_round(
                wire_messages,
                assistant_text=current_assistant_text,
                round_tool_calls=current_round_calls,
                round_results=round0_results,
            )
            self._maybe_inject_question_images(wire_messages, round0_results)
            # Turn-internal live ctx refresh: round 0 just completed with a
            # provider-measured wire size — push it so the main badge updates
            # immediately (before the first follow-up round streams). Skipped
            # when the round produced no measurement (helper returns None).
            _ctx_frame = _context_usage_frame(seq=seq + 1)
            if _ctx_frame is not None:
                seq += 1
                yield _ctx_frame

        # ── Kernel-driven follow-up rounds (kernel round k == stream k) ───
        # Closures carry the main loop's专有 state; the kernel stays
        # conv-agnostic. ``_round_rid`` is the snapshot id for the round the
        # kernel is currently opening (set in ``on_round_open``); the emitter
        # stamps the round's frames with it.
        _round_rid: str | None = None
        _no_progress_text: str | None = None

        # ── Mid-turn user injection seam (V2 enhancement) ─────────────────
        # ``_inject_hook`` runs inside the kernel's inter-round gap (after the
        # abort check + compaction, before the send wire is built). It drains
        # the tab's pending injections, persists each as a ``role:user``
        # message (so a history reload shows it), and stashes ``{id, text}``
        # into ``_injected_pending`` so the emitter forwards an
        # ``injected_message`` frame on the matching ``KernelRoundStarted``.
        # It returns the texts the kernel appends to the wire so the model
        # sees them on this round. ``None`` registry ⇒ no-op (feature off).
        _injected_pending: list[dict[str, Any]] = []

        async def _inject_hook(round_no: int) -> list[InjectedContent]:
            registry = self._injection_registry
            if registry is None:
                return []
            texts = registry.drain(tab.id)
            if not texts:
                return []
            store = self._image_upload_store
            contents: list[InjectedContent] = []
            for text in texts:
                injected_msg = self._build_message(
                    role=MessageRole.USER,
                    content=MessageContent(text=text),
                    now=self._clock.now(),
                    parent_id=(
                        conv.last_message().id if conv.last_message() else None
                    ),
                    meta={"injected": True},
                )
                # Record the minted message for the caller to fold into
                # ``state.injected_messages`` (persisted AFTER the turn's
                # frame-rebuild via ``_reinsert_injected_messages``). We do NOT
                # ``conv.append_message`` here: the per-round persist truncates
                # everything past the baseline and rebuilds from frames, so a
                # direct append would be dropped on the next save.
                if injected_messages_out is not None:
                    injected_messages_out.append((round_no, injected_msg))
                _injected_pending.append(
                    {
                        "id": injected_msg.id.value,
                        "text": text,
                        "round_no": round_no,
                    }
                )
                # Image parity: an injection carries its images the SAME way a
                # normal submit / a question-answer does — as ``![](url)``
                # markdown inlined in the text. Resolve those refs to
                # OpenAI-Vision blocks (same helper + store the question-answer
                # image path uses, streaming.py:4065) so the model actually
                # sees the image; the resolver strips the markdown from the
                # text block, leaving clean text + image block(s). Fall back to
                # plain text when there is no image / no store / refs do not
                # resolve (defensive — never drop the user's text).
                image_refs = _extract_image_refs(text)
                vision_blocks: list[dict[str, Any]] = []
                if image_refs and store is not None:
                    vision_blocks = _resolve_image_refs_to_vision_blocks(
                        store=store,
                        image_refs=image_refs,
                        source_text=text,
                    )
                contents.append(vision_blocks if vision_blocks else text)
            return contents

        async def _compact_hook(
            round_no: int, wire: list[dict[str, Any]]
        ) -> list[dict[str, Any]] | None:
            nonlocal compaction_token, compressed_history_override
            prev_override = compressed_history_override
            prev_compaction_token = compaction_token
            # Thread this turn's MOST-RECENT completed round's provider-measured
            # ``实发`` (last_round_prompt_tokens +cache_read for Anthropic) into
            # the trigger gate: mid-turn this round's usage is NOT yet on
            # conv.messages, so the conv-derived path cannot see it. ``实发`` is
            # the size actually being sent THIS turn → the correct compaction
            # judgement. Uses the shared ``assistant_eff_prompt``口径.
            # CCD-2 (PENDING-WORK.md §1): ``last_round_usage`` is the CURRENT
            # turn's most-recent round usage — its source model IS the current
            # request's model, so ``request.model_hint`` is the correct family
            # discriminator here (no historical model-switch ambiguity).
            _measured_eff: int | None = None
            if isinstance(last_round_usage, dict):
                _measured_eff = _assistant_eff_prompt(
                    last_round_usage,
                    _is_anthropic_family(request.model_hint or ""),
                )
            compaction_token = await self._maybe_compress_round(
                round_no=round_no,
                conv=conv,
                request=request,
                compression_token=compaction_token,
                live_wire=wire,
                measured_eff_prompt=_measured_eff,
                live_completed_rounds=completed_rounds,
            )
            _did_compact = compaction_token is not prev_compaction_token
            if _did_compact:
                # The checkpoint's ``compacted_wire`` now contains every PRIOR
                # in-flight round — clear ``completed_rounds`` so the rebuild
                # below does NOT replay them on top (duplication trap).
                completed_rounds.clear()
            # The synthetic-retry override is swapped in by ``on_round_end``
            # (it sets ``compressed_history_override``); rebuild when either it
            # or the compaction token changed.
            if not (
                _did_compact
                or compressed_history_override is not prev_override
            ):
                return None
            nonlocal _snapshot_turn_ref
            if _did_compact:
                ckpt = self._compaction_checkpoints.get(self._conv_key(conv))
                base_wire = (
                    [dict(m) for m in ckpt.compacted_wire]
                    if ckpt is not None
                    else self._assemble_history_wire(conv=conv, request=request)
                )
                rebuilt = base_wire
            else:
                rebuilt = self._build_base_wire_messages(
                    conv=conv,
                    request=request,
                    compressed_history=compressed_history_override,
                )
            for _cr in completed_rounds:
                self._append_tool_round(
                    rebuilt,
                    assistant_text=_cr.text,
                    round_tool_calls=_cr.tool_calls,
                    round_results=_cr.tool_results,
                )
            # Base history changed → new wire is no longer a prefix of earlier
            # rounds' snapshots; start a fresh shared-prefix turn segment.
            _snapshot_turn_ref = str(uuid.uuid4())
            return rebuilt

        async def _on_round_open(
            round_no: int, send_wire: list[dict[str, Any]]
        ) -> None:
            nonlocal _round_rid
            _round_rid = None
            # ── Old-tool-output aging (prune) ─────────────────────────────────
            # ``send_wire`` is the kernel's ``build_send_wire`` output — a FRESH
            # per-round list of the bytes about to be sent (NOT the growing
            # ``wire_messages`` the compaction replay owns, NOT persisted
            # history). Right before it becomes ``extra["messages"]`` (the
            # payload handed to ``self._llm.stream``) we age out old ``role:tool``
            # result bodies that fall outside the recent ~40K-token protect
            # window when the reclaimable total ≥ 20K tokens, replacing them with
            # a ``[Old tool result content cleared]`` placeholder. The transform
            # is best-effort + idempotent + returns a NEW list, so re-applying it
            # per round is safe and never touches the loop's ``wire_messages`` or
            # the durable conversation history. Aging BEFORE we snapshot keeps
            # the persisted round snapshot faithful to the real sent bytes
            # (State-Truth-First: the snapshot == what the model actually saw).
            #
            # 方案B gate: only age when the registry says this gateway has NO
            # prompt cache (or no registry wired = prior unconditional behaviour).
            # Unknown (first round) / supports-cache → SKIP aging so the cached
            # prefix stays byte-clean and the whole history replays as a cache
            # read. When skipped, ``send_wire`` passes through unchanged →
            # ``_aged_oldest_run`` naturally measures 0 below (no placeholders) →
            # the cache-breakpoint baseline is 0 → the adapter falls back to its
            # default "last message" anchor, which is correct for a stable
            # un-aged prefix (核实5: 不冲突).
            if (
                self._provider_cache_registry is None
                or self._provider_cache_registry.aging_enabled(request.model_hint)
            ):
                send_wire = age_old_tool_outputs(
                    send_wire, model_hint=request.model_hint,
                    min_aged_tool_count=self._aged_tool_lowerbound.get(
                        self._conv_key(conv)
                    ) or None,
                )
            # ── Monotonic aged-tool boundary (改动2b) ─────────────────────────
            # After aging, measure how many of the OLDEST ``role:tool`` results
            # are now placeholders (the contiguous aged run from the oldest tool
            # message forward). Advance the per-conversation lower-bound to
            # ``max(prev, measured)`` — it only ever GROWS, so the low prefix is
            # byte-stable across rounds and next round's ``min_aged_tool_count``
            # re-freezes at least this much. Forward this COUNT (not a raw
            # index) to the adapter as the cache breakpoint anchor: the adapter
            # re-derives the anchor against its FINAL wire, so the count is
            # robust to the intervening system-prompt insert / orphan-tool
            # repair / sanitisation that would invalidate a raw index. This
            # touches ONLY the SEND-wire copy + the cache breakpoint hint —
            # never ``wire_messages`` / ``conv.messages`` (durable UI history
            # untouched: hard red-line, no 回看 regression).
            _conv_key = self._conv_key(conv)
            _aged_oldest_run = 0
            for _m in send_wire:
                if not (isinstance(_m, dict) and _m.get("role") == "tool"):
                    continue
                _c = _m.get("content")
                if isinstance(_c, str) and _c == AGED_TOOL_OUTPUT_PLACEHOLDER:
                    _aged_oldest_run += 1
                else:
                    break
            _prev_lb = self._aged_tool_lowerbound.get(_conv_key, 0)
            _new_lb = max(_prev_lb, _aged_oldest_run)
            if _new_lb != _prev_lb:
                self._aged_tool_lowerbound[_conv_key] = _new_lb
            # ── Cache-breakpoint anchor baseline (改动: 平稳期缓存闭环) ─────────
            # The count forwarded to the adapter's cache breakpoint MUST stay
            # constant across a compaction epoch, otherwise the breakpoint drifts
            # backward every round (as ``_aged_oldest_run`` grows with the sliding
            # 40K protect window) → the cache prefix lengthens → Anthropic sees a
            # different prefix → cache MISS every steady-state round. We therefore
            # FREEZE a per-conversation baseline and only re-base it when a
            # compaction produces a NEW checkpoint (detected by the checkpoint's
            # ``anchor_index`` signature changing — the compacted head is replaced
            # that round, so the breakpoint SHOULD move then anyway; that one
            # round's miss is expected & accepted). Within an epoch the baseline
            # is held constant → byte-stable breakpoint → cache_read hits the
            # stable [compacted-head + baseline-aged-prefix] region every round.
            # This ONLY touches the in-memory baseline dict + the cache hint value
            # passed to the adapter — never ``wire_messages`` / ``conv.messages``
            # (durable UI history untouched: hard red-line, no 回看 regression).
            _ckpt_now = self._compaction_checkpoints.get(_conv_key)
            _ckpt_sig: int | None = (
                _ckpt_now.anchor_index if _ckpt_now is not None else None
            )
            _prev_sig = self._last_seen_checkpoint_sig.get(_conv_key, None)
            _sig_known = _conv_key in self._last_seen_checkpoint_sig
            if (not _sig_known) or _ckpt_sig != _prev_sig:
                # First observation for this conversation OR a compaction just
                # produced a new checkpoint → (re)base the frozen baseline to the
                # freshly measured oldest-aged run. (On the very first round with
                # no aging yet this is 0, and 改动 below falls back to the live
                # run so we still cache whatever stable oldest prefix exists.)
                self._cache_anchor_baseline[_conv_key] = _aged_oldest_run
                self._last_seen_checkpoint_sig[_conv_key] = _ckpt_sig
            # First-aging freeze (残留漂移修复): before any compaction has happened,
            # the baseline may still be 0 (set to _aged_oldest_run=0 at first
            # observation when nothing was aged yet). Once aging FIRST produces
            # placeholders (_aged_oldest_run > 0) while the baseline is still 0,
            # freeze it NOW instead of falling back to the live run every round
            # (which would grow with the sliding protect window → breakpoint drift
            # → early-round cache miss). This makes the pre-first-compaction steady
            # state byte-stable too. Only advances 0→N once; subsequent growth is
            # ignored until a compaction re-bases (same as post-compaction epochs).
            # The frozen value is "the oldest-aged run of the round aging first
            # produced placeholders" — those oldest placeholders are then held by
            # the monotonic lower-bound (_aged_tool_lowerbound, set above) so their
            # prefix bytes stay stable. Touches ONLY the in-memory baseline dict.
            if (
                self._cache_anchor_baseline.get(_conv_key, 0) <= 0
                and _aged_oldest_run > 0
            ):
                self._cache_anchor_baseline[_conv_key] = _aged_oldest_run
            # Build the round's LLM request ONCE here (system prompt, sampling,
            # tools, followup_round / tool_results overrides) with the kernel's
            # SEND wire as ``extra["messages"]``. ``_open_round_stream`` reuses
            # the stashed request, and the per-round prompt snapshot uses its
            # FULL ``extra`` (carrying the resolved system-prompt context the
            # snapshot needs) — parity with the old inline order.
            extra_overrides: dict[str, Any] = {
                "followup_round": round_no,
                "tool_results": list(tool_results),
            }
            if len(send_wire) > 0:
                extra_overrides["messages"] = list(send_wire)
            # Forward the cache-breakpoint anchor COUNT (改动2b + 平稳期闭环) to
            # the LLM adapter as an adapter-internal control key (``__``-prefixed →
            # popped in ``_build_payload`` before the wire forward loop, so it
            # never reaches the on-wire body). We forward the FROZEN per-
            # conversation baseline (set/re-based above only right after a
            # compaction), NOT the live ``_aged_oldest_run`` — so the breakpoint
            # position is byte-stable across the whole compaction epoch and the
            # cache actually hits every steady-state round. ``0`` → omitted → the
            # adapter keeps its prior "last message" cache breakpoint scan
            # (byte-for-byte, main-agent early rounds before any aging).
            _anchor = self._cache_anchor_baseline.get(_conv_key, 0)
            # Fresh conversation with a baseline of 0 (no aging captured yet):
            # fall back to the current run so we still cache whatever stable
            # oldest prefix exists this round.
            if _anchor <= 0:
                _anchor = _aged_oldest_run
            if _anchor > 0:
                extra_overrides["__cache_stable_aged_prefix__"] = _anchor
            llm_req = await self._build_llm_request(
                conv=conv,
                tab=tab,
                request=request,
                extra_overrides=extra_overrides,
                compressed_history=compressed_history_override,
            )
            _round_req["req"] = llm_req
            if round_request_ids is None:
                return
            _round_rid = await self._save_round_snapshot(
                wire_messages=send_wire,
                extra=(llm_req.extra or {}),
                request=request,
                model_hint=request.model_hint,
                turn_ref=_snapshot_turn_ref,
            )
            if _round_rid is not None:
                round_request_ids[round_no] = _round_rid

        def _open_round_stream(
            *, round_no: int, send_wire: list[dict[str, Any]]
        ) -> AsyncIterator[StreamFrame]:
            # Open the LLM stream from the request ``_on_round_open`` just built
            # (so the snapshot + the stream share one request — no duplicate
            # ``_build_llm_request``). The kernel drains the stream; its drain
            # tees each TOOL_CALL frame and (with ``forward_tool_calls_inline``)
            # yields ``KernelToolCallSeen`` so the emitter forwards it inline
            # (byte-for-byte order — TOOL_CALL interleaved with chunks).
            llm_req = _round_req["req"]

            async def _gen() -> AsyncIterator[StreamFrame]:
                # Re-opened on every NETWORK retry: a fresh ``self._llm.stream``
                # off the SAME request/wire (no content streamed yet on a
                # connect failure, so re-opening never duplicates output). The
                # backoff sleep is abortable via ``handle``.
                async for frame in self._network_retrying_stream(
                    lambda: self._llm.stream(llm_req), handle
                ):
                    yield frame

            # Blocked-read Stop failsafe for FOLLOW-UP rounds (root-cause 2).
            # The kernel's ``_drain_round`` only polls ``abort_check`` BETWEEN
            # frames (``async for frame in stream``); if the upstream cloud SSE
            # stalls for a long time between chunks (common when several tabs
            # contend for the same cloud quota), the bare ``__anext__`` blocks
            # and a user Stop cannot interrupt it until httpx's 600s read-timeout
            # — the round (and its tool card) hangs for minutes. The INITIAL
            # stream already races each ``__anext__`` against the abort handle
            # via :meth:`_abortable_frames`; the follow-up rounds historically
            # did NOT, so we wrap the per-round stream with the SAME guard here
            # (V1 parity: V1's front-end agent loop had a 60s per-round read
            # race + a 15s server keepalive; moving the loop server-side must
            # not drop that responsiveness).
            return self._abortable_frames(_gen(), handle)

        def _tool_executor(
            *,
            round_no: int,
            tool_metas: list[tuple[str, dict[str, Any], str]],
        ) -> AsyncIterator[ToolExecutionItem]:
            # The kernel's tool-execution producer: dispatch THIS round's tool
            # calls via the SAME shell dispatcher the main loop always used
            # (agent recursion streaming + exec partials + parallel gather all
            # live inside ``_dispatch_pending_tool_calls``). Each StreamFrame it
            # yields is wrapped in a ToolExecutionItem carrying the ORIGINAL
            # frame so the emitter forwards it byte-for-byte: a partial exec
            # TOOL_RESULT → partial item; a final TOOL_RESULT → final item; a
            # SUBAGENT_*/AGENT_SUMMARY frame → passthrough item.
            return self._dispatch_round_as_producer(
                pending_tool_calls=list(_round_pending["frames"]),
                guardrail=guardrail,
                tab=tab,
                conv=conv,
                request=request,
                tool_results=tool_results,
                handle=handle,
                round_index=round_no,
            )

        async def _grow_wire_hook(
            round_no: int,
            assistant_text: str,
            tool_metas: list[tuple[str, dict[str, Any], str]],
            finals: list[ToolExecutionItem],
        ) -> None:
            # Main loop owns wire growth: record the round's block into
            # ``completed_rounds`` (for compaction replay) and append it via
            # the SAME ``_append_tool_round`` (id precedence / thought_signature
            # / ``content:None`` vs sentinel) so the outbound wire is identical.
            #
            # Kernel call order per round (real, per
            # ``_single_agent_turn.py:Kernel.run``):
            #
            #     compact_hook → stream → on_round_end (line 753)
            #       → _execute_tool_round (line 826)
            #       → grow_wire_hook (line 842)
            #
            # So by the time we run, ``_on_round_end`` has ALREADY captured
            # THIS round's ``end_payload["usage"]`` into the nonlocal
            # ``last_round_usage`` (streaming.py:6720-6721). We read it
            # directly and fill the new ``_CompletedRound`` IN PLACE — no
            # placeholder, no separate event-loop backfill. This is the fix
            # for the "double-callback seam off-by-one" bug: a backfill in
            # ``_on_round_end`` runs BEFORE this hook every round, so the
            # entry the backfill saw was always the PREVIOUS round's — which
            # let round N+1's ``prompt_tokens`` leak onto round N's slot.
            # See ``docs/90-refactor/CONTEXT-COMPRESSION.md``
            # §3.2 (revised) and the regression test
            # ``tests/unit/qai/chat/test_completed_rounds_seam_ordering.py``.
            #
            # ``_prev_real`` is the previous round's cumulative
            # ``real_prompt_tokens``; after a mid-turn compaction
            # ``completed_rounds.clear()`` (streaming.py:6334-6339) it is 0,
            # which IS the right baseline for the new post-compaction wire
            # (per special-point D). State-Truth-First (AGENTS.md 铁律 1):
            # cloud usage is the real measurement; tokenizer fallback only
            # runs on THIS round's incremental content (single bounded
            # pass), never the whole wire.
            round_results = tool_results[_round_results_before["n"]:]
            if not round_results:
                return
            _prev_real = (
                completed_rounds[-1].real_prompt_tokens
                if completed_rounds
                else 0
            )
            _real, _comp, _src = self._extract_round_real_prompt(
                usage=(
                    last_round_usage
                    if isinstance(last_round_usage, dict)
                    else None
                ),
                new_tool_results=round_results,
                assistant_text=assistant_text,
                model_hint=request.model_hint,
                prev_real_prompt=_prev_real,
            )
            completed_rounds.append(
                _CompletedRound(
                    text=assistant_text,
                    tool_calls=list(_round_pending["frames"]),
                    tool_results=list(round_results),
                    real_prompt_tokens=_real,
                    completion_tokens=_comp,
                    source=_src,
                )
            )
            self._append_tool_round(
                wire_messages,
                assistant_text=assistant_text,
                round_tool_calls=list(_round_pending["frames"]),
                round_results=round_results,
            )
            self._maybe_inject_question_images(wire_messages, round_results)

        async def _on_round_end(
            round_no: int,
            end_payload: dict[str, Any],
            round_text: str,
            tool_calls: list[dict[str, Any]],
        ) -> RoundEndDecision:
            nonlocal empty_completion_retry_used, compressed_history_override
            nonlocal last_tool_round, _last_call_sig, _repeat_count
            nonlocal _no_progress_text
            nonlocal last_round_usage
            nonlocal _budget_exceeded
            nonlocal _budget_used_at_stop, _budget_max_at_stop
            nonlocal _budget_local_hinted, _budget_local_hint_pending
            # DIAG (Problem B — turn_usage prompt_tokens explodes to ~10M):
            # ``_accumulate_usage`` SUMs every integer usage key — including
            # ``prompt_tokens`` / ``total_tokens`` — across every agentic round.
            # For cumulative-prompt providers (Anthropic/Claude in particular:
            # each round RE-SENDS the full conversation + all prior tool
            # results), ``prompt_tokens`` is already the running wire size, so
            # summing it round-over-round is QUADRATIC (round 0 ~50K + round 1
            # ~80K + ... = millions by round 17). This log surfaces the fold's
            # before/delta/after for ``prompt_tokens`` + ``total_tokens`` per
            # round so the user can watch exactly where the SUM blows up. The
            # per-round CORRECT wire size is captured separately as
            # ``last_round_usage`` below (read via ``last_round_prompt_tokens``);
            # the summed ``prompt_tokens``/``total_tokens`` is what ends up on the
            # END frame + persisted ``usage`` dict (streaming.py ~8260) and reads
            # as the bogus 10M figure in ``chat.diag.tail_append_usage``.
            _round_usage_in = end_payload.get("usage")
            # 方案B: learn this gateway's prompt-cache support from the round's
            # raw usage signal. ``_extract_usage`` emits ``provider_reported_cache
            # = True`` ONLY when the gateway echoed a cache field (majority
            # no-cache path omits the key to stay byte-for-byte identical). Since
            # ``_extract_usage`` ALWAYS computes the signal on the real path, a
            # real usage dict WITHOUT the key means "gateway did NOT report cache"
            # → mark no-cache (enables next-round aging). ``True`` → supports cache
            # → keep aging OFF. The signal is derived from RAW BEFORE the
            # corrected branch zeros cache_read_tokens, so it is not masked by the
            # eff-prompt correction (the坑). We only mark when a usage dict is
            # present (a round with no usage at all leaves capability unchanged).
            if self._provider_cache_registry is not None and isinstance(
                _round_usage_in, dict
            ):
                self._provider_cache_registry.mark(
                    request.model_hint,
                    bool(_round_usage_in.get("provider_reported_cache")),
                )
            _before_pt = int(usage_accumulator.get("prompt_tokens", 0) or 0)
            _before_tt = int(usage_accumulator.get("total_tokens", 0) or 0)
            # Accumulate this round's usage into the running turn total.
            self._accumulate_usage(usage_accumulator, _round_usage_in)
            try:
                _delta_pt = 0
                _delta_tt = 0
                if isinstance(_round_usage_in, dict):
                    _delta_pt = int(_round_usage_in.get("prompt_tokens") or 0)
                    _delta_tt = int(_round_usage_in.get("total_tokens") or 0)
                _log.info(
                    "chat.diag.turn_usage_fold",
                    round_no=round_no,
                    is_anthropic=_is_anthropic_family(request.model_hint or ""),
                    prompt_tokens_before=_before_pt,
                    prompt_tokens_delta=_delta_pt,
                    prompt_tokens_after=int(
                        usage_accumulator.get("prompt_tokens", 0) or 0
                    ),
                    total_tokens_before=_before_tt,
                    total_tokens_delta=_delta_tt,
                    total_tokens_after=int(
                        usage_accumulator.get("total_tokens", 0) or 0
                    ),
                )
            except Exception:  # noqa: BLE001 — diagnostics must never break a turn
                pass
            # Keystone capture: remember THIS round's usage as the last seen
            # (overwritten each round) so the running full-history counter can
            # read the LAST round's true wire prompt size, not the cross-round
            # sum in ``usage_accumulator``. ALSO: ``_grow_wire_hook`` fires
            # AFTER us in the kernel order (``_single_agent_turn.py:Kernel.run``
            # line 753 vs 842) and reads this nonlocal directly to fill the
            # new ``_CompletedRound`` with the provider-measured prompt size
            # — see ``docs/90-refactor/CONTEXT-COMPRESSION.md``
            # §3.2 (revised) for the seam contract. ``_extract_usage`` already
            # corrected ``prompt_tokens`` to ``max(prompt, total - completion)``
            # (llm_stream.py:1917-1921) so cache-hit turns report the real
            # wire size, not the under-reported delta. Terminal rounds (no
            # tool_calls) trigger ``KernelFinished`` before ``_grow_wire_hook``
            # ever runs (``_single_agent_turn.py`` line 788-802), so they
            # correctly produce NO ``_CompletedRound`` entry — the
            # differential path has no atomic group to attribute.
            _round_u = end_payload.get("usage")
            if isinstance(_round_u, dict):
                last_round_usage = _round_u
            # ── Per-conversation token-budget bookkeeping (max_budget_tokens) ──
            # Fold THIS round's PROVIDER-MEASURED net-new tokens into the
            # conversation's shared budget pool, then decide whether to STOP the
            # loop before opening another round (DECISION: allow this round —
            # already sent — to finish; do not start the next once the cap is
            # met). State-Truth-First (AGENTS.md 铁律 1): use ONLY the real
            # per-round measurement (``last_round_*`` — single round, NEVER the
            # cumulative ``turn_usage`` SUM which is Anthropic-corrupt), never an
            # estimate. Local (on-device) turns emit no usage → skip enforcement
            # entirely (NPU produces no authoritative count; emit a one-per-turn
            # advisory hint instead). Gemini partial-reliability: a round whose
            # usage reports prompt_tokens=0 AND completion_tokens=0 is not
            # enforced (log a warning) — no truth to enforce against.
            if _is_local_model_hint(request.model_hint):
                if not _budget_local_hinted:
                    _budget_local_hinted = True
                    _budget_local_hint_pending = True
                    _log.debug(
                        "chat.budget_local_skip",
                        conversation_id=getattr(
                            getattr(conv, "id", None), "value", None
                        ),
                        model_hint=request.model_hint,
                    )
            else:
                _bt = self._budget
                _br = await _bt.check(conv.id)
                if _br.enabled:
                    # Compute this round's net-new tokens from the REAL usage.
                    _lru = (
                        last_round_usage
                        if isinstance(last_round_usage, dict)
                        else None
                    )
                    _raw_pt = (
                        int(_lru.get("prompt_tokens") or 0) if _lru else 0
                    )
                    _comp = (
                        int(_lru.get("completion_tokens") or 0) if _lru else 0
                    )
                    if _lru is not None and _raw_pt == 0 and _comp == 0:
                        # Gemini partial reliability: no measurement this round.
                        _log.warning(
                            "chat.budget_round_no_usage",
                            round_no=round_no,
                            conversation_id=conv.id.value,
                            model_hint=request.model_hint,
                        )
                    else:
                        _eff_pt = _effective_prompt_tokens(
                            _lru,
                            is_anthropic=_is_anthropic_family(
                                request.model_hint or ""
                            ),
                            include_cache_write_fallback=True,
                        )
                        _delta = _eff_pt + _comp
                        if _delta > 0:
                            await _bt.observe(conv.id, _delta)
                            _br = await _bt.check(conv.id)
                    if _br.exceeded:
                        _budget_exceeded = True
                        _budget_used_at_stop = int(_br.used or 0)
                        _budget_max_at_stop = int(_br.max_tokens or 0)
                        _log.info(
                            "chat.budget_exceeded",
                            round_no=round_no,
                            conversation_id=conv.id.value,
                            used_tokens=_br.used,
                            max_tokens=_br.max_tokens,
                        )
                        # Stop the loop WITHOUT executing this round's pending
                        # calls; the emitter renders the budget notice + a
                        # terminal END(reason="budget_exceeded"). If this round
                        # had NO pending calls the turn ends naturally anyway
                        # (the flag still steers the terminal reason below).
                        if bool(tool_calls):
                            return RoundEndDecision(stop=True, final_text="")
            has_pending = bool(tool_calls)
            text_clean = round_text.strip()
            reason = end_payload.get("reason")
            is_stop = reason in (None, "completed", "stop")
            # Empty-completion retry (PR-091 H-9 / V1 chat_handler.py:757).
            if (
                is_stop
                and not text_clean
                and not has_pending
                and last_tool_round > 0
                and not empty_completion_retry_used
                and round_no > 1
            ):
                _log.info(
                    "chat.empty_completion_retry",
                    round_no=round_no,
                    last_tool_round=last_tool_round,
                )
                synthetic = await self._build_synthetic_retry_history(
                    conv=conv,
                    compressed_history=compressed_history_override,
                    synthetic_user_text="请基于工具结果总结回答。",
                )
                empty_completion_retry_used = True
                compressed_history_override = synthetic
                # The next round's ``compact_hook`` sees the changed override
                # and rebuilds the wire from the synthetic-nudge base; pass the
                # CURRENT wire as the immediate swap so the retry round's send
                # wire already reflects it.
                return RoundEndDecision(
                    retry=True, retry_wire=list(wire_messages)
                )
            if has_pending:
                # This round issued tool calls → it ran a tool; run the
                # no-progress detection on this batch's signature.
                last_tool_round = round_no
                _sig = _tool_calls_signature(_round_pending["frames"])
                if _sig != "" and _sig == _last_call_sig:
                    _repeat_count += 1
                else:
                    _repeat_count = 1
                    _last_call_sig = _sig
                if _repeat_count >= _NO_PROGRESS_LIMIT:
                    _log.warning(
                        "chat.followup_no_progress_break",
                        round_no=round_no,
                        repeats=_repeat_count,
                        request_id=getattr(request, "request_id", None),
                    )
                    _no_progress_text = (
                        "\n\n_(检测到工具调用在原地重复、未取得进展，已自动结束本轮。"
                        "可能是上下文不完整导致；请重述需求或换个说法让我重试。)_"
                    )
                    return RoundEndDecision(stop=True, final_text="")
                return RoundEndDecision()
            # Terminal END (no pending calls): schedule experience extraction.
            self._schedule_experience_extraction(
                round_no=round_no,
                last_tool_round=last_tool_round,
                conv=conv,
                tab=tab,
            )
            return RoundEndDecision()

        # Per-round mutable holders shared between hooks (the kernel calls them
        # in a fixed per-round order, so tiny holders thread the round's pending
        # frames + the result-baseline from open→execute→grow).
        _round_pending: dict[str, list[StreamFrame]] = {"frames": []}
        _round_results_before: dict[str, int] = {"n": 0}
        _round_req: dict[str, Any] = {"req": None}

        # Per-conversation budget short-circuit (max_budget_tokens): if folding
        # round 0's usage already put the conversation AT/OVER the cap (e.g. it
        # was already over from a prior turn, or round 0 alone blew the budget),
        # do NOT open the follow-up loop at all — round 0 has fired (allow-
        # overshoot), so honestly stop here with an explanatory notice + a
        # terminal END(reason="budget_exceeded"). This is the DECISION's "fire
        # round 0, then if already over, short-circuit before firing another".
        if _budget_exceeded:
            seq += 1
            yield StreamFrame.chunk(
                frame_id=f"fu-budget-{seq}",
                sequence=seq,
                text=(
                    "\n\n_(已达到本会话的 token 预算上限。可在弹出的对话框中选择"
                    "「继续」（自动调高上限）或「停止」。)_"
                ),
                round_index=0,
            )
            seq += 1
            _b0_usage: dict[str, int] | None = None
            if usage_accumulator:
                _b0_usage = self._append_display_usage_fields(
                    self._finalize_turn_usage(
                        usage_accumulator, last_round_usage, request.model_hint
                    ),
                    last_round_usage,
                    initial_usage,
                )
            yield StreamFrame.end(
                frame_id=f"fu-budget-end-{seq}",
                sequence=seq,
                reason="budget_exceeded",
                usage=_b0_usage,
                extra=self._budget_decision_payload(
                    used_tokens=_budget_used_at_stop,
                    max_tokens=_budget_max_at_stop,
                ),
            )
            return

        async for kev in self._kernel.run(
            wire_messages=wire_messages,
            open_round_stream=_open_round_stream,
            tool_executor=_tool_executor,
            # The main loop derives metas from the round's tee'd FRAMES (the
            # kernel passes its collected payloads, ignored here); the metas
            # only drive pairing — the wire is grown by ``_grow_wire_hook``.
            build_tool_metas=lambda _calls, _rn: self._frames_to_metas(
                _round_pending["frames"]
            ),
            max_rounds=self._max_followup_rounds,
            abort_check=handle.is_set,
            model_hint=request.model_hint,
            compact_hook=_compact_hook,
            on_round_open=_on_round_open,
            on_round_end=_on_round_end,
            grow_wire_hook=_grow_wire_hook,
            inject_hook=_inject_hook,
            forward_tool_calls_inline=True,
        ):
            if isinstance(kev, KernelRoundStarted):
                # Reset per-round holders; ``KernelToolCallSeen`` events fill
                # ``_round_pending["frames"]`` as the stream drains.
                _round_pending["frames"] = []
                _round_results_before["n"] = len(tool_results)
                # V1 useChat.js:2318/2392-2405 parity: each follow-up LLM round
                # gets its own ``content`` slot — the final assistant message
                # persists only the LAST round's text. Clear the accumulator
                # the outer ``_run`` folds chunk text into so a model that
                # re-emits its lead-in every round does not concatenate
                # "<lead-in><lead-in>" into the final persisted message.
                assistant_text_parts.clear()
                # Per-conversation budget (max_budget_tokens): if the PREVIOUS
                # round was a local (NPU) turn, the budget is not enforceable
                # there (no authoritative usage) — surface a once-per-turn
                # advisory as a marked chunk so the user knows local turns are
                # neither counted nor capped. Drained here (not inside
                # ``_on_round_end``, which returns a value and cannot yield).
                if _budget_local_hint_pending:
                    _budget_local_hint_pending = False
                    seq += 1
                    yield StreamFrame.chunk(
                        frame_id=f"fu-budget-local-{seq}",
                        sequence=seq,
                        text=(
                            "\n\n_(本地模型无法统计 token 用量，"
                            "本轮不计入也不受 token 预算限制。)_"
                        ),
                        round_index=kev.round_no,
                    )
                # ran just before this ``KernelRoundStarted`` (the inter-round
                # seam), draining + persisting any pending injection(s) and
                # stashing them in ``_injected_pending``. Forward one
                # ``injected_message`` frame per injection so the frontend
                # commits its pending grey bubble into a real user message
                # BEFORE this round's chunks/tool cards stream in.
                if _injected_pending:
                    for _inj in _injected_pending:
                        seq += 1
                        yield StreamFrame.injected_message(
                            frame_id=f"fu-inject-{seq}",
                            sequence=seq,
                            text=_inj["text"],
                            message_id=_inj.get("id"),
                            round_index=_inj.get("round_no"),
                        )
                    _injected_pending.clear()
                # Turn-internal live ctx refresh (V2 enhancement): the PREVIOUS
                # round's ``_grow_wire_hook`` already appended its
                # ``_CompletedRound`` (with the provider-measured
                # ``real_prompt_tokens``) before this ``KernelRoundStarted``
                # fires (kernel call order: grow_wire_hook of round k → compact
                # → on_round_open → KernelRoundStarted of round k+1). So by now
                # ``completed_rounds[-1]`` holds the latest measured wire size —
                # push a ``context_usage`` frame so the main-conversation badge
                # tracks the real growth WHILE the turn runs. Emits NOTHING when
                # the last round produced no measurement (helper returns None).
                _ctx_frame = _context_usage_frame(seq=seq + 1)
                if _ctx_frame is not None:
                    seq += 1
                    yield _ctx_frame
                continue
            if isinstance(kev, KernelChunk):
                seq += 1
                yield _stamp_request_id(
                    _stamp_round(
                        StreamFrame.chunk(
                            frame_id=f"fu-chunk-{seq}",
                            sequence=seq,
                            text=kev.text,
                        ),
                        kev.round_no,
                    ),
                    _round_rid,
                )
                continue
            if isinstance(kev, KernelToolCallSeen):
                # Inline TOOL_CALL forwarding (byte-for-byte order parity) +
                # stash the frame for ``_append_tool_round`` / signatures.
                _round_pending["frames"].append(kev.frame)
                yield _stamp_request_id(
                    _stamp_round(kev.frame, kev.round_no), _round_rid
                )
                seq += 1
                continue
            if isinstance(kev, KernelToolCallsIssued):
                # TOOL_CALL frames already forwarded inline above — nothing to
                # emit here (the event just marks the execute boundary).
                continue
            if isinstance(kev, (KernelToolPartial, KernelToolResult)):
                # Forward the ORIGINAL dispatcher frame byte-for-byte (results
                # bound to the ISSUING round == this round + its request_id).
                if kev.frame is not None:
                    yield _stamp_request_id(
                        _stamp_round(kev.frame, kev.round_no), _round_rid
                    )
                    seq += 1
                continue
            if isinstance(kev, KernelStreamPassthrough):
                # Drain-phase progress frame (cloud ``generating_args`` partial
                # tool_result): forward the ORIGINAL frame byte-for-byte, stamped
                # to this round + its request_id like any other live frame, so
                # the UI shows the tool card early with a live arg preview during
                # a long argument generation (parity with the pre-kernel
                # ``_drain_followup_round`` that yielded it directly). Pure UI
                # progress — it is NOT collected into ``_round_pending`` /
                # ``tool_results`` and never grows the wire.
                if kev.frame is not None:
                    yield _stamp_request_id(
                        _stamp_round(kev.frame, kev.round_no), _round_rid
                    )
                    seq += 1
                continue
            if isinstance(kev, KernelError):
                # A follow-up round's LLM stream surfaced an ERROR frame (e.g. a
                # transient ``chat.llm.connect_error`` on the post-tool-call
                # request). The kernel's ``_drain_round`` consumed the original
                # ERROR (+ its trailing END) and handed it back on ``kev.frame``
                # — UNLIKE round 0, where ``_drain_main_stream`` forwards the
                # ERROR inline. So we MUST re-emit it here, or the turn ends
                # silently with a misleading ``done`` and no retry hint (the
                # reported "执行完工具就停止了" bug). Re-emit the original ERROR
                # frame verbatim (preserving code / message / retryable) stamped
                # to this round + its request_id, then a terminal END(failed) so
                # the WS/SSE layer closes the turn as a failure rather than a
                # success. ``state.had_llm_error`` is set by the caller's
                # follow-up consumer when it sees this ERROR frame (so
                # ``_finalize_turn`` does not overwrite it with empty_response).
                if kev.frame is not None:
                    seq += 1
                    yield _stamp_request_id(
                        _stamp_round(kev.frame, kev.round_no), _round_rid
                    )
                seq += 1
                yield StreamFrame.end(
                    frame_id=f"fu-err-end-{seq}",
                    sequence=seq,
                    reason="failed",
                )
                return
            if isinstance(kev, KernelAborted):
                return
            if isinstance(kev, KernelFinished):
                # Surface the keystone last-round usage to the caller (it owns
                # ``state``; this generator does not). Mutating the supplied
                # holder in place keeps the END frame wire shape unchanged.
                if last_round_usage_out is not None and last_round_usage:
                    last_round_usage_out.clear()
                    last_round_usage_out.update(last_round_usage)
                # Drain a pending local-turn budget advisory (a local FINAL round
                # ends the turn here without a following KernelRoundStarted).
                if _budget_local_hint_pending:
                    _budget_local_hint_pending = False
                    seq += 1
                    yield StreamFrame.chunk(
                        frame_id=f"fu-budget-local-{seq}",
                        sequence=seq,
                        text=(
                            "\n\n_(本地模型无法统计 token 用量，"
                            "本轮不计入也不受 token 预算限制。)_"
                        ),
                        round_index=kev.round_no,
                    )
                # Per-conversation token-budget cap reached (max_budget_tokens):
                # a completed round pushed cumulative usage at/over the cap, so
                # we do NOT open another round (the DECISION: allow the in-flight
                # round to finish, then stop). Emit a friendly notice + a
                # terminal ``END(reason="budget_exceeded")`` carrying the turn's
                # usage so the client can render the same token badge + a
                # budget-specific toast. Checked BEFORE the no-progress / normal
                # branches because the budget stop is the authoritative reason
                # for THIS turn's early close.
                if _budget_exceeded:
                    seq += 1
                    yield StreamFrame.chunk(
                        frame_id=f"fu-budget-{seq}",
                        sequence=seq,
                        text=(
                            "\n\n_(已达到本会话的 token 预算上限，已在本轮结束后"
                            "停止。如需继续，请在预算设置中调高上限或重置用量。)_"
                        ),
                        round_index=kev.round_no,
                    )
                    seq += 1
                    _budget_usage: dict[str, int] | None = None
                    if usage_accumulator:
                        _budget_usage = self._append_display_usage_fields(
                            self._finalize_turn_usage(
                                usage_accumulator,
                                last_round_usage,
                                request.model_hint,
                            ),
                            last_round_usage,
                            initial_usage,
                        )
                    yield StreamFrame.end(
                        frame_id=f"fu-budget-end-{seq}",
                        sequence=seq,
                        reason="budget_exceeded",
                        usage=_budget_usage,
                        extra=self._budget_decision_payload(
                            used_tokens=_budget_used_at_stop,
                            max_tokens=_budget_max_at_stop,
                        ),
                    )
                    return
                if _no_progress_text is not None:
                    # Graceful no-progress close (Fix B): friendly chunk + a
                    # normal END so the turn closes via ``confirmDone``.
                    seq += 1
                    yield StreamFrame.chunk(
                        frame_id=f"fu-noprog-{seq}",
                        sequence=seq,
                        text=_no_progress_text,
                        round_index=kev.round_no,
                    )
                    seq += 1
                    yield StreamFrame.end(
                        frame_id=f"fu-noprog-end-{seq}",
                        sequence=seq,
                        reason="followup_no_progress",
                    )
                    return
                # Normal terminal END: surface the turn usage. ``completion_tokens``
                # / ``cache_read_tokens`` are the cross-round SUM (correct), but
                # ``prompt_tokens`` / ``total_tokens`` are corrected to the LAST
                # round's true wire size for cumulative-prompt providers
                # (Anthropic/Claude RE-SEND the full history each round, so summing
                # their per-round prompt is quadratic — the ~10M bug). Single-round
                # turns and non-anthropic providers are unchanged numerically.
                # See ``_finalize_turn_usage``. (V1 useChat.js accumulation parity
                # is preserved for the additive keys.)
                if usage_accumulator:
                    seq += 1
                    _final_usage = self._finalize_turn_usage(
                        usage_accumulator,
                        last_round_usage,
                        request.model_hint,
                    )
                    # VERIFY (Problem 3 — usage accumulation fix): contrast the
                    # raw cross-round SUM (the ~10M bug value) against the
                    # corrected END-frame usage. ``raw_sum_prompt`` is what the
                    # bug emitted; ``final_prompt`` should equal the LAST round's
                    # wire size for anthropic (e.g. ~135K, NOT millions). If
                    # ``final_prompt`` still balloons into the millions the fix
                    # did not take; if it tracks the last round it is working.
                    _log.info(
                        "chat.diag.turn_usage_finalized",
                        is_anthropic=_is_anthropic_family(
                            request.model_hint or ""
                        ),
                        raw_sum_prompt=int(
                            usage_accumulator.get("prompt_tokens", 0) or 0
                        ),
                        raw_sum_total=int(
                            usage_accumulator.get("total_tokens", 0) or 0
                        ),
                        final_prompt=int(
                            _final_usage.get("prompt_tokens", 0) or 0
                        ),
                        final_total=int(
                            _final_usage.get("total_tokens", 0) or 0
                        ),
                        final_completion=int(
                            _final_usage.get("completion_tokens", 0) or 0
                        ),
                    )
                    # SHARED tail-append口径 (DRY): give the LIVE END frame the
                    # SAME ``last_round_*`` / display fields the persisted
                    # message carries (:meth:`_finalize_assistant_message`). Was
                    # the root cause of the live-vs-reload ↑ drift: the END frame
                    # used to omit ``last_round_cache_read_display`` so the badge
                    # fell back to cacheRead=0 and summed the whole prompt (e.g.
                    # 7324+7506=14830) until a reload re-read the persisted usage
                    # (→ 3+3=6). ``last_round_usage`` here is the terminal round's
                    # raw usage dict (round-0 for a single-round turn, seeded at
                    # :8138 / overwritten per round at ``_on_round_end``).
                    # PER-ROUND ↑ FIX: also pass ``initial_usage`` (round-0's
                    # own usage) as ``first_round_usage`` so the LIVE END frame
                    # carries ``first_round_cache_*_display`` too — a main-agent
                    # 2-round turn ("创建子Agent说hello") LIVE ↑ then sums Round 1's
                    # write-turn net-new (~3) + Round 2's read-turn net-new (~1) =
                    # ~4, matching the reload path (persisted message threads the
                    # same first_round via :3585→:4529) instead of only showing
                    # the final round's ~0/1. When ``initial_usage`` is absent the
                    # helper falls back to ``last_round`` (口径-identical to
                    # first===last → front-end counts it once). DISPLAY-ONLY; the
                    # prompt/total correction from _finalize_turn_usage above is
                    # untouched, cache_read_tokens stays zeroed. §3.1.
                    _final_usage = self._append_display_usage_fields(
                        _final_usage, last_round_usage, initial_usage
                    )
                    yield StreamFrame.end(
                        frame_id=f"fu-end-{seq}",
                        sequence=seq,
                        reason=kev.end_payload.get("reason", "completed"),
                        usage=_final_usage,
                        request_id=kev.end_payload.get("request_id"),
                    )
                    return
                # No usage figure (no round produced one): re-emit a plain END
                # carrying the model's reason (parity with the prior drain that
                # forwarded the model END unchanged when no usage accumulated).
                seq += 1
                yield StreamFrame.end(
                    frame_id=f"fu-end-{seq}",
                    sequence=seq,
                    reason=kev.end_payload.get("reason", "completed"),
                    request_id=kev.end_payload.get("request_id"),
                )
                return
            if isinstance(kev, KernelMaxRoundsReached):
                # Round budget exhausted while tool calls still pending.
                seq += 1
                notice = (
                    f"\n\n_(已达到工具循环上限 {self._max_followup_rounds} 轮，"
                    "本轮提前结束。如需继续可让我接着上次的进度做。)_"
                )
                yield StreamFrame.chunk(
                    frame_id=f"fu-cap-{seq}",
                    sequence=seq,
                    text=notice,
                    round_index=self._max_followup_rounds,
                )
                seq += 1
                yield StreamFrame.end(
                    frame_id=f"fu-end-{seq}",
                    sequence=seq,
                    reason="followup_rounds_exhausted",
                )
                return


    def _frames_to_metas(
        self, frames: list[StreamFrame]
    ) -> list[tuple[str, dict[str, Any], str]]:
        """Derive ``(name, args, call_id)`` metas from a round's TOOL_CALL frames.

        Only used to drive the kernel's :class:`KernelToolCallsIssued` /
        final-pairing — the main loop's outbound wire is grown by
        ``_grow_wire_hook`` (``_append_tool_round``), which derives its OWN ids
        from the result dicts; so the precise ids here only need to pair the
        kernel's finals (by ``tool_call_id``) and never enter the wire.
        """
        metas: list[tuple[str, dict[str, Any], str]] = []
        for idx, fr in enumerate(frames):
            payload = fr.payload if isinstance(fr.payload, dict) else {}
            name = payload.get("tool_name") or "tool"
            args = payload.get("arguments")
            call_id = payload.get("tool_call_id") or f"fu_{fr.frame_id}_{idx}"
            metas.append((name, args if isinstance(args, dict) else {}, call_id))
        return metas

    def _dispatch_round_as_producer(
        self,
        *,
        pending_tool_calls: list[StreamFrame],
        guardrail: GuardrailPort,
        tab: Any,
        conv: Any,
        request: "StreamChatInput",
        tool_results: list[dict[str, Any]],
        handle: StreamAbortHandle,
        round_index: int | None = None,
    ) -> AsyncIterator[ToolExecutionItem]:
        """Run a round's tool dispatch as the kernel's tool-execution producer.

        Reuses the SAME shell dispatcher the main loop always used
        (:meth:`_dispatch_pending_tool_calls` — agent recursion streaming +
        exec partials + parallel gather). Each StreamFrame it yields is wrapped
        in a :class:`ToolExecutionItem` carrying the ORIGINAL frame so the
        emitter forwards it byte-for-byte:

        * a ``partial=True`` exec TOOL_RESULT → a partial item (live UI);
        * a final TOOL_RESULT → a final item (one per executed call);
        * a SUBAGENT_* / AGENT_SUMMARY frame (from an ``agent`` dispatch) → a
          ``passthrough`` item the kernel forwards but does not count as a
          final.

        The dispatcher mutates ``tool_results`` in place (the main loop's
        ``_grow_wire_hook`` reads the new slice); the abort handle is honoured
        inside ``_dispatch_pending_tool_calls`` via its ``outcome``.

        ``round_index`` (optional, V2 UX FIX) is the parent agent's round
        number for this tool-execution batch. It is threaded down to
        ``iter_events`` and ends up on the ``subagent_start`` frame's
        payload so the front-end can route per-round SUBAGENT_START to a
        FRESH per-round message — without it, two sub-agents spawned in
        different rounds of the same turn (A round 0, B round 1) collapse
        onto the same parent message and B's ``index=0`` de-dup drops A.
        Omitted when the caller does not know its round (test paths).
        """

        async def _gen() -> AsyncIterator[ToolExecutionItem]:
            outcome = _FollowupRoundOutcome(seq=0)
            async for fr in self._dispatch_pending_tool_calls(
                pending_tool_calls=pending_tool_calls,
                guardrail=guardrail,
                tab=tab,
                conv=conv,
                request=request,
                tool_results=tool_results,
                handle=handle,
                outcome=outcome,
                round_index=round_index,
            ):
                if fr.frame_type is StreamFrameType.TOOL_RESULT:
                    is_partial = bool(fr.payload.get("partial"))
                    cid = fr.payload.get("tool_call_id") or fr.frame_id
                    tname = fr.payload.get("tool_name") or "tool"
                    if is_partial:
                        yield ToolExecutionItem(
                            call_id=str(cid),
                            tool_name=str(tname),
                            partial=True,
                            delta=str(fr.payload.get("delta") or ""),
                            frame=fr,
                        )
                    else:
                        yield ToolExecutionItem(
                            call_id=str(cid),
                            tool_name=str(tname),
                            partial=False,
                            result_text=str(fr.payload.get("result") or ""),
                            ok=bool(fr.payload.get("ok", True)),
                            frame=fr,
                        )
                else:
                    # SUBAGENT_* / AGENT_SUMMARY / any other frame the agent
                    # dispatch emits → forward verbatim (not a tool result).
                    yield ToolExecutionItem(
                        call_id=fr.frame_id,
                        tool_name="",
                        passthrough=True,
                        frame=fr,
                    )

        return _gen()

    async def _maybe_compress_round(
        self,
        *,
        round_no: int,
        conv: Any,
        request: "StreamChatInput",
        compression_token: Any,
        live_wire: list[dict[str, Any]] | None = None,
        measured_eff_prompt: int | None = None,
        live_completed_rounds: list["_CompletedRound"] | None = None,
    ) -> Any:
        """Every 4th round, run inter-round context compaction (checkpoint).

        Slice of :meth:`_run_followup_loop` (B1 cohesion split).  Mirrors
        legacy ``backend/chat_handler.py:708-722`` (PR-091 B-2 / H-2):
        ``round_no`` starts at 1 so compaction triggers on rounds 4, 8,
        12, ...

        ROOT FIX (compaction made effective): instead of the old
        ``_compress_history`` (which dropped tool output by only taking
        ``msg.content.text``), this runs :meth:`_compress_via_checkpoint`,
        which estimates + compresses the assembled含-tool-output WIRE and
        stores a session :class:`CompactionCheckpoint` (``conv.messages``
        untouched).  Returns a CHANGE TOKEN: a fresh sentinel when a NEW
        checkpoint was stored (so the loop rebuilds its growing
        ``wire_messages`` from the new compacted base), else the unchanged
        ``compression_token`` (best-effort — failure never advances).

        MID-TURN ENHANCEMENT: ``live_wire`` is the loop's authoritative
        ``wire_messages`` (含 every in-flight tool round this turn).  Passing
        it lets a single super-long agentic turn be compressed mid-flight
        (the in-flight rounds are folded into the checkpoint's
        ``compacted_wire``).  When this returns a NEW token the caller MUST
        rebuild ``wire_messages`` from the checkpoint AND clear
        ``completed_rounds`` (they are now inside ``compacted_wire`` — see the
        rebuild branch in :meth:`_run_followup_loop`).

        ``measured_eff_prompt``: this turn's most-recent completed round's
        provider-measured ``实发`` size, threaded through to the trigger gate
        (this turn's usage is not yet on ``conv.messages`` mid-turn).
        """
        if not (
            round_no > 1
            and round_no % 4 == 0
            and self._context_compressor is not None
        ):
            return compression_token
        try:
            changed = await self._compress_via_checkpoint(
                conv=conv,
                request=request,
                target_ratio=_COMPRESS_TARGET_RATIO,
                live_wire=live_wire,
                measured_eff_prompt=measured_eff_prompt,
                live_completed_rounds=live_completed_rounds,
            )
        except Exception as exc:  # noqa: BLE001 — compression is best-effort
            _log.warning(
                "chat.followup_compression_failed",
                round_no=round_no,
                error=str(exc),
            )
            return compression_token
        return object() if changed else compression_token

    async def _dispatch_pending_tool_calls(
        self,
        *,
        pending_tool_calls: list[StreamFrame],
        guardrail: GuardrailPort,
        tab: Any,
        conv: Any,
        request: "StreamChatInput",
        tool_results: list[dict[str, Any]],
        handle: StreamAbortHandle,
        outcome: "_FollowupRoundOutcome",
        round_index: int | None = None,
    ) -> AsyncIterator[StreamFrame]:
        """Dispatch one round's pending tool calls; yield their frames.

        Tool-dispatch slice of :meth:`_run_followup_loop` (B1 cohesion
        split).  Byte-for-byte identical to the inline body:

        * PR-subagent-stream: ``agent`` tool calls are split out and run
          via :meth:`_dispatch_agent_calls_streaming` (parallel sub-agents
          + SUBAGENT_* frames) when the sub-agent event-stream port is
          wired; everything else goes through
          :meth:`_execute_single_tool_call`;
        * the consolidated agent text still flows back into
          ``tool_results`` (mutated in place) so the parent loop continues.

        The abort handle short-circuits with ``outcome.aborted`` at every
        original check point; ``outcome.seq`` carries the advanced
        sequence counter back to the caller.
        """
        seq = outcome.seq
        # ── PR-subagent-stream: separate ``agent`` tool calls from the
        # rest.  When the sub-agent event-stream port is wired we
        # dispatch agent calls via streaming events (V1
        # chat_handler.py:597-694 parity) so the UI can render an
        # in-progress sub-agent block; the consolidated text still flows
        # back into ``tool_results`` so the parent agentic loop continues.
        agent_tc_frames: list[StreamFrame] = []
        other_tc_frames: list[StreamFrame] = []
        if self._agent_event_stream is not None:
            for tc_frame in pending_tool_calls:
                name = tc_frame.payload.get("tool_name")
                if isinstance(name, str) and name == "agent":
                    agent_tc_frames.append(tc_frame)
                else:
                    other_tc_frames.append(tc_frame)
        else:
            other_tc_frames = list(pending_tool_calls)

        if agent_tc_frames:
            # Inner generator stamps each yielded frame with a
            # locally-incrementing sequence starting at ``seq``; we
            # advance the outer ``seq`` by one per frame so a subsequent
            # ``StreamFrame.tool_result`` keeps ascending without
            # collision.
            async for fr in self._dispatch_agent_calls_streaming(
                tab=tab,
                conv=conv,
                agent_tc_frames=agent_tc_frames,
                handle=handle,
                request=request,
                tool_results=tool_results,
                seq_start=seq,
                round_index=round_index,
            ):
                if handle.is_set():
                    outcome.aborted = True
                    outcome.seq = seq
                    return
                yield fr
                seq += 1
            if handle.is_set():
                outcome.aborted = True
                outcome.seq = seq
                return

        # PARALLEL-TOOL-1: non-``agent`` tool calls in one round now run
        # CONCURRENTLY (was a serial ``for``). Per-call ordering contract
        # (parallel-tool-execution-design.md §1): frames of different
        # ``tool_call_id`` MAY interleave, but each call's partials precede its
        # single final (guaranteed because each call's own streaming generator
        # is sequential). ``tool_results`` is spliced back in ORIGINAL order
        # (§2.4). ``frame_id``/``sequence`` get disjoint per-call bases so the
        # ids never collide while interleaving; the merge loop re-stamps the
        # outer ``seq`` monotonically as it yields.
        async for fr in self._dispatch_tool_calls_parallel_streaming(
            other_tc_frames=other_tc_frames,
            tab=tab,
            conv=conv,
            guardrail=guardrail,
            request=request,
            tool_results=tool_results,
            handle=handle,
            seq_start=seq,
            outcome=outcome,
        ):
            if handle.is_set():
                outcome.aborted = True
                outcome.seq = seq
                return
            yield fr
            seq += 1
        outcome.seq = seq

    def _cancelled_tool_message(self, request: "StreamChatInput") -> str:
        """The user-facing + model-facing text for a per-tool cancellation.

        Per the product decision the SAME text is shown on the tool card and
        fed back to the model. Language follows the conversation: a coarse
        CJK check on the user's message (mirrors ``intent_metrics.detect_language``
        without importing it) picks Simplified Chinese vs English — good enough
        for a short status line; the model then continues the turn in whatever
        language it is already using.
        """
        text = getattr(request.user_message, "text", "") or ""
        is_zh = any("\u4e00" <= ch <= "\u9fff" for ch in text)
        return _build_cancelled_tool_message(is_zh=is_zh)

    def _synth_cancelled_result(
        self,
        *,
        tc_frame: StreamFrame,
        request: "StreamChatInput",
        seq: int,
        started_ms: int | None,
        tool_results: list[dict[str, Any]],
    ) -> StreamFrame:
        """Append a ``[cancelled]`` LLM-facing result dict + build its frame.

        Called when the user cancels ONE tool. It records the cancellation in
        ``tool_results`` (so the next LLM round sees it, ``ok=False``) and
        returns a terminal ``tool_result`` frame carrying ``cancelled=True`` so
        the UI can render the card as cancelled. Crucially this does NOT touch
        the abort handle / ``outcome.aborted`` — the turn keeps going.
        """
        tool_name = tc_frame.payload.get("tool_name") or ""
        call_id = tc_frame.payload.get("tool_call_id")
        msg = self._cancelled_tool_message(request)
        tool_results.append(
            {
                "tool_name": tool_name,
                "tool_call_id": call_id,
                "arguments": dict(tc_frame.payload.get("arguments") or {}),
                "result": msg,
                "ok": False,
                "truncated": False,
                "cancelled": True,
                "thought_signature": tc_frame.payload.get("thought_signature"),
            },
        )
        duration = (
            max(0, _now_ms(self._clock) - started_ms)
            if started_ms is not None
            else None
        )
        return StreamFrame.tool_result(
            frame_id=f"fu-tr-{seq}",
            sequence=seq,
            tool_name=tool_name,
            result=msg,
            partial=False,
            cancelled=True,
            tool_call_id=call_id,
            duration_ms=duration,
        )

    async def _dispatch_tool_calls_parallel_streaming(
        self,
        *,
        other_tc_frames: list[StreamFrame],
        tab: Any,
        conv: Any,
        guardrail: GuardrailPort,
        request: "StreamChatInput",
        tool_results: list[dict[str, Any]],
        handle: StreamAbortHandle,
        seq_start: int,
        outcome: "_FollowupRoundOutcome",
    ) -> AsyncIterator[StreamFrame]:
        """Run a round's non-``agent`` tool calls CONCURRENTLY, merge frames.

        PARALLEL-TOOL-1 (parallel-tool-execution-design.md). Each call's
        existing :meth:`_execute_tool_call_streaming` generator (partials +
        final, exec streaming, guardrail, operator hooks, truncation, timing)
        runs unchanged but CONCURRENTLY:

        * each generator writes its result into a PRIVATE per-slot
          ``tool_results`` list; the shared ``tool_results`` is extended in
          ORIGINAL slot order only after all calls finish (§2.4 — the LLM sees
          results in the order it issued the calls, not completion order);
        * frames are merged through a queue so different calls' partials may
          interleave, while within one call partials still precede its final
          (each generator is sequential — per-call ordering contract §1);
        * each call gets a disjoint ``seq`` base (stride) so ``frame_id`` /
          ``sequence`` never collide across concurrent calls; the caller
          re-stamps the monotonic outer ``seq`` as it re-yields;
        * abort: each drain task checks ``handle`` per frame; the merge loop
          polls so a user Stop interrupts promptly.

        A single call (the overwhelmingly common case) takes a fast path that
        is byte-for-byte identical to the old serial loop (no queue overhead).
        """
        # Drop malformed frames (no usable tool_name) up front — parity with
        # the old loop's "yields nothing, no seq advance".
        live_frames = [
            f
            for f in other_tc_frames
            if isinstance(f.payload.get("tool_name"), str)
            and f.payload.get("tool_name")
        ]
        if not live_frames:
            return

        # Fast path: a single tool call → no concurrency machinery, identical
        # to the historical serial behaviour. Still takes a budget slot so a
        # single heavy exec respects the shared budget (cheap when unbounded).
        if len(live_frames) == 1:
            only = live_frames[0]
            only_name = only.payload.get("tool_name") or ""
            cm = (
                self._tool_concurrency.slot(only_name)
                if self._tool_concurrency is not None
                else _null_async_ctx()
            )
            async with cm:
                # ORPHAN-KILL FIX: guard the per-call streaming generator with
                # ``aclosing`` so the ``return`` on a user Stop below
                # DETERMINISTICALLY ``aclose()``-s it. A bare ``async for`` +
                # ``return`` does NOT close the async generator it was
                # iterating (CPython only calls ``__anext__``); teardown is
                # then deferred to GC / the loop's async-gen finalizer. That
                # left ``_execute_tool_call_streaming`` — and through it the
                # live exec subprocess stream — un-closed on abort, so the
                # child kept running (39 s / 129 s) and no terminal frame was
                # produced (the tool card stayed "执行中" with a Stop button
                # even after the tab was closed + reopened). ``aclosing`` makes
                # the close synchronous with the Stop, which cascades into
                # ``_execute_tool_call_streaming``'s own ``aclosing`` guard →
                # the exec engine's child tree-kill.
                async with contextlib.aclosing(
                    self._execute_tool_call_streaming(
                        tc_frame=only,
                        tab=tab,
                        conv=conv,
                        guardrail=guardrail,
                        request=request,
                        tool_results=tool_results,
                        seq=seq_start,
                    )
                ) as _only_stream:
                    # ROOT-CAUSE FIX (主 Agent 单工具快速路径挂死): the old loop
                    # was a bare ``async for tr_frame in _only_stream`` with a
                    # PER-FRAME ``handle.is_set()`` check. A hung/idle ``exec``
                    # (no stdout for a long time) parks ``__anext__`` on the
                    # subprocess read, so the per-frame check never runs and a
                    # user Stop cannot interrupt until the exec engine's own
                    # timeout fires (or NEVER when ``timeout=0``). Unlike the
                    # parallel branch below, this fast path had no
                    # ``gather_task.cancel()`` failsafe. We now race a SINGLE
                    # in-flight ``__anext__`` against a short abort poll: on Stop
                    # we cancel the pending fetch and return, which — because the
                    # iterator is under ``aclosing`` — DETERMINISTICALLY
                    # ``aclose()``-s ``_execute_tool_call_streaming`` → its inner
                    # ``aclosing`` (chunk_iter) → ``stream_exec``'s finally →
                    # subprocess TREE kill (tool_exec_stream.py), so the child
                    # dies immediately and the round unwinds without waiting for
                    # the timeout. The fast path stays a single-call path (no
                    # queue / no gather over multiple calls) — we only add the
                    # cancel capability. CRITICAL: exactly ONE ``__anext__`` is
                    # ever in flight at a time (a fresh fetch is created only
                    # AFTER the previous one resolved), so we never drive the
                    # async generator concurrently or drop a frame.
                    _ANEXT_POLL_S = 0.05
                    _anext_task: "asyncio.Task[Any] | None" = None
                    _only_call_id = only.payload.get("tool_call_id") or ""
                    _only_started_ms = _now_ms(self._clock)
                    _consume_cancel = getattr(handle, "consume_cancel_tool", None)
                    try:
                        while True:
                            if handle.is_set():
                                outcome.aborted = True
                                return
                            # Per-call cancel (single-tool path): the user hit
                            # THIS card's stop. Unlike a whole-turn abort we do
                            # NOT set ``outcome.aborted`` — we cancel just this
                            # tool, emit a synthesized ``[cancelled]`` terminal
                            # result (fed back to the model), and return so the
                            # turn continues to the next round.
                            if (
                                _consume_cancel is not None
                                and _only_call_id
                                and _consume_cancel(_only_call_id)
                            ):
                                yield self._synth_cancelled_result(
                                    tc_frame=only,
                                    request=request,
                                    seq=seq_start,
                                    started_ms=_only_started_ms,
                                    tool_results=tool_results,
                                )
                                return
                            if _anext_task is None:
                                _anext_task = asyncio.ensure_future(
                                    _only_stream.__anext__()
                                )
                            done, _pending = await asyncio.wait(
                                {_anext_task}, timeout=_ANEXT_POLL_S
                            )
                            if _anext_task not in done:
                                # Poll window elapsed with no frame yet — keep the
                                # SAME pending fetch and re-check the abort handle
                                # at the top of the loop (do NOT create a second
                                # ``__anext__``).
                                continue
                            try:
                                tr_frame = _anext_task.result()
                            except StopAsyncIteration:
                                # Stream exhausted normally → leave the loop so
                                # the ``aclosing`` context exits cleanly.
                                _anext_task = None
                                break
                            finally:
                                # The fetch resolved (value / StopAsyncIteration /
                                # error) — clear it so the next iteration starts a
                                # fresh one. On error the exception already
                                # propagated out of ``.result()`` above.
                                if _anext_task is not None and _anext_task.done():
                                    _anext_task = None
                            if handle.is_set():
                                outcome.aborted = True
                                return
                            yield tr_frame
                    finally:
                        # Any exit (normal break, Stop ``return``, an outer
                        # cancel raised through ``await asyncio.wait`` /
                        # ``yield``, or a stream error) must not leak a pending
                        # ``__anext__``: cancel + reap it so the ``aclosing``
                        # teardown of ``_only_stream`` (and the exec tree-kill it
                        # cascades into) is not blocked on an orphaned fetch.
                        if _anext_task is not None and not _anext_task.done():
                            _anext_task.cancel()
                            try:
                                await _anext_task
                            except (
                                asyncio.CancelledError,
                                StopAsyncIteration,
                                Exception,  # noqa: BLE001 — reap noise only
                            ):
                                pass
            return

        # --- Parallel path (≥2 calls) --------------------------------------
        # Disjoint seq base per call so concurrent frames never collide on
        # ``frame_id``/``sequence`` (the merge re-stamps the outer seq anyway).
        _SEQ_STRIDE = 100_000
        n = len(live_frames)
        # One private result list per slot; spliced back in order at the end.
        slot_results: list[list[dict[str, Any]]] = [[] for _ in range(n)]
        queue: asyncio.Queue[tuple[int, StreamFrame] | None] = asyncio.Queue()

        async def _drain_one(slot: int, tc_frame: StreamFrame) -> None:
            tool_name = tc_frame.payload.get("tool_name") or ""
            slot_call_id = tc_frame.payload.get("tool_call_id") or ""
            slot_started_ms = _now_ms(self._clock)
            consume_cancel = getattr(handle, "consume_cancel_tool", None)
            cm = (
                self._tool_concurrency.slot(tool_name)
                if self._tool_concurrency is not None
                else _null_async_ctx()
            )
            try:
                async with cm:
                    # ORPHAN-KILL FIX (parallel path): same ``aclosing`` guard
                    # as the single-tool fast path — a ``return`` on Stop (or
                    # this ``_drain_one`` task being cancelled by the merge
                    # loop's ``gather_task.cancel()``) must DETERMINISTICALLY
                    # close the per-call streaming generator so the live exec
                    # subprocess is torn down instead of orphaned.
                    async with contextlib.aclosing(
                        self._execute_tool_call_streaming(
                            tc_frame=tc_frame,
                            tab=tab,
                            conv=conv,
                            guardrail=guardrail,
                            request=request,
                            tool_results=slot_results[slot],
                            seq=seq_start + slot * _SEQ_STRIDE,
                        )
                    ) as _slot_stream:
                        _anext_task: "asyncio.Task[Any] | None" = None
                        try:
                            while True:
                                if handle.is_set():
                                    return
                                # Per-call cancel: the user stopped THIS tool's
                                # card. Cancel only this slot, synthesize a
                                # ``[cancelled]`` result into this slot's private
                                # results (spliced back at the end) + emit its
                                # terminal frame, then return. The OTHER slots
                                # keep running and the turn continues (no
                                # ``outcome.aborted``).
                                if (
                                    consume_cancel is not None
                                    and slot_call_id
                                    and consume_cancel(slot_call_id)
                                ):
                                    fr = self._synth_cancelled_result(
                                        tc_frame=tc_frame,
                                        request=request,
                                        seq=seq_start + slot * _SEQ_STRIDE,
                                        started_ms=slot_started_ms,
                                        tool_results=slot_results[slot],
                                    )
                                    await queue.put((slot, fr))
                                    return
                                if _anext_task is None:
                                    _anext_task = asyncio.ensure_future(
                                        _slot_stream.__anext__()
                                    )
                                done, _pending = await asyncio.wait(
                                    {_anext_task}, timeout=0.05
                                )
                                if _anext_task not in done:
                                    continue
                                try:
                                    tr_frame = _anext_task.result()
                                except StopAsyncIteration:
                                    _anext_task = None
                                    break
                                finally:
                                    if (
                                        _anext_task is not None
                                        and _anext_task.done()
                                    ):
                                        _anext_task = None
                                await queue.put((slot, tr_frame))
                                if handle.is_set():
                                    return
                        finally:
                            # ORPHAN-KILL FIX (parallel path, per-call cancel /
                            # abort return): mirror the single-tool fast path —
                            # any exit (per-call cancel ``return``, abort
                            # ``return``, an outer cancel, or a stream error)
                            # must NOT leave a pending ``__anext__`` in flight.
                            # If it did, exiting ``aclosing(_slot_stream)`` would
                            # call ``aclose()`` on a generator that is still
                            # "running" (parked in that pending fetch) → CPython
                            # raises ``RuntimeError: aclose(): asynchronous
                            # generator is already running`` and the live exec
                            # subprocess tree-kill (in ``_execute_tool_call_streaming``'s
                            # ``finally``) would NOT run synchronously with the
                            # cancel — the child would be orphaned. Cancel + reap
                            # the pending fetch first so ``aclose()`` closes
                            # cleanly and the subprocess dies with the cancel.
                            if _anext_task is not None and not _anext_task.done():
                                _anext_task.cancel()
                                try:
                                    await _anext_task
                                except (
                                    asyncio.CancelledError,
                                    StopAsyncIteration,
                                    Exception,  # noqa: BLE001 — reap noise only
                                ):
                                    pass
            except Exception as exc:  # noqa: BLE001 — isolate one call's failure
                _log.warning(
                    "chat.parallel_tool_drain_failed",
                    slot=slot,
                    error=str(exc),
                )

        async def _run_all() -> None:
            await asyncio.gather(
                *(_drain_one(i, f) for i, f in enumerate(live_frames)),
                return_exceptions=False,
            )
            await queue.put(None)

        gather_task = asyncio.create_task(_run_all())
        _POLL = 0.5
        try:
            while True:
                if handle.is_set():
                    outcome.aborted = True
                    break
                if gather_task.done() and queue.empty():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_POLL)
                except asyncio.TimeoutError:
                    continue
                if item is None:
                    break
                _slot, fr = item
                if handle.is_set():
                    outcome.aborted = True
                    break
                yield fr
        finally:
            if not gather_task.done():
                gather_task.cancel()
                try:
                    await gather_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        # Splice each call's result entries back into the shared list in
        # ORIGINAL slot order (design §2.4) so the LLM sees results in the
        # order it issued the calls. Skipped on abort (partial round).
        if not handle.is_set():
            for entries in slot_results:
                tool_results.extend(entries)

    async def _dispatch_agent_calls_streaming(
        self,
        *,
        tab: Any,
        conv: Any,
        agent_tc_frames: list[StreamFrame],
        handle: StreamAbortHandle,
        request: "StreamChatInput",
        tool_results: list[dict[str, Any]],
        seq_start: int,
        round_index: int | None = None,
    ) -> AsyncIterator[StreamFrame]:
        """Run agent tool_calls concurrently and yield SUBAGENT_* + AGENT_SUMMARY.

        V1 parity: ``backend/chat_handler.py:597-694`` — when the LLM
        emits one or more ``agent`` tool_calls in a single round, the
        sub-agents run **in parallel** via :func:`asyncio.gather`, their
        events are merged through a queue into the parent stream, and
        a single ``agent_summary`` marker is emitted before the parent
        agent's follow-up text.

        ``tool_results`` is mutated in place: one entry per agent call,
        carrying the consolidated final text so the next LLM round
        sees the agent results in ``extra["tool_results"]`` exactly
        like a regular tool dispatch (the LLM does not need to know it
        was a sub-agent — V1 same).
        """
        assert self._agent_event_stream is not None
        seq = seq_start
        total = len(agent_tc_frames)

        # Whether the sub-agents spawned THIS turn are themselves allowed to
        # create (grand) sub-agents. Driven by the per-(main-agent)-tab
        # "allow_child_spawn" switch the SSE/WS layer placed in ``request.extra``
        # (default off → historical hard recursion guard). When True, each
        # spawned sub-agent receives the ``agent`` tool (schema + handler) so it
        # may spawn ONE further level; the sub-agents IT spawns default back to
        # ``allow_spawn=False`` (see ``agent_tool._iter_loop``), so the tree
        # stops at the next level unless the user opts that level in too — i.e.
        # this switch controls ONLY the main agent's direct children, exactly
        # as documented. This is also the recursion-safety封顶: spawning never
        # cascades without an explicit per-level opt-in.
        allow_child_spawn = bool((request.extra or {}).get("allow_child_spawn"))

        # ── Run all agents concurrently and merge events via a queue ──
        # Mirrors V1 chat_handler.py:642-656.  Each sub-agent worker
        # drains its ``iter_events`` async iterator and pushes events
        # plus a ``(idx, final_result_text)`` sentinel for tool_results
        # accounting.  ``None`` is the all-done sentinel.
        queue: asyncio.Queue[Any] = asyncio.Queue()
        # Per-index final result text (consolidated done.text or
        # error message) for ``tool_results`` accounting.
        final_results: dict[int, str] = {}
        # Per-index resumable sub-agent session id (from ``subagent_done``),
        # surfaced back to the main agent so it can wake the SAME sub-agent.
        final_subagent_ids: dict[int, str] = {}

        async def _drain_one(
            idx: int,
            tc_frame: StreamFrame,
        ) -> None:
            try:
                args = tc_frame.payload.get("arguments") or {}
                if not isinstance(args, dict):
                    args = {}
                inv_request = ToolInvocationRequest(
                    tab_id=tab.id,
                    conversation_id=conv.id,
                    tool_name="agent",
                    arguments=dict(args),
                )
                # Optional wake handle (V2): the LLM may pass
                # ``resume_subagent_id`` to continue a sub-agent it spawned
                # earlier (with its prior memory). Parsed defensively — an
                # invalid / over-long id is ignored so the sub-agent simply
                # starts fresh (never errors the turn).
                resume_id: SubAgentSessionId | None = None
                raw_resume = args.get("resume_subagent_id")
                if isinstance(raw_resume, str) and raw_resume:
                    try:
                        resume_id = SubAgentSessionId(raw_resume)
                    except (ValueError, TypeError):
                        resume_id = None
                # Optional profile selector (V2): the LLM may pass
                # ``subagent_type`` (``general`` default / ``explore``
                # read-only). Forwarded verbatim — the handler resolves an
                # unknown/None value to ``general`` (historical behaviour) and,
                # on a resume, ignores it in favour of the persisted profile.
                raw_subagent_type = args.get("subagent_type")
                subagent_type = (
                    raw_subagent_type
                    if isinstance(raw_subagent_type, str)
                    else None
                )
                # Optional human-readable name (V2 UX enhancement): the LLM may
                # pass ``name`` — a short 3-5 word task label the UI shows as
                # the sub-agent card title (instead of the generic
                # ``SubAgent N`` fallback). Forwarded verbatim to the handler,
                # which persists it as ``SubAgentSession.title`` and echoes it
                # on the ``subagent_start`` frame. None ⇒ UI falls back.
                raw_name = args.get("name")
                subagent_name = (
                    raw_name if isinstance(raw_name, str) and raw_name else None
                )
                # ``allow_spawn`` is forwarded ONLY when the main agent
                # enabled child-spawning (default off ⇒ omit the kwarg so the
                # call is byte-identical to the historical path — light
                # ``SubAgentEventStreamPort`` test doubles need not declare the
                # optional kwarg, per the Protocol's documented minimal
                # surface). The reference impl (``AgentToolHandler``) accepts it.
                _spawn_kwargs: dict[str, Any] = (
                    {"allow_spawn": True} if allow_child_spawn else {}
                )
                # Forward the per-session ("this conversation only") disabled
                # tool set so the sub-agent inherits the user's
                # SessionToolsPopover choices (advertise filter + execution
                # gate inside the sub-agent). Passed only when non-empty so the
                # call stays byte-identical to the historical path for sessions
                # without an override (light ``SubAgentEventStreamPort`` test
                # doubles need not declare the optional kwarg). The reference
                # impl (``AgentToolHandler``) accepts it.
                _disabled_tools_list = sorted(
                    _session_disabled_tools(request.extra)
                    | _persona_disabled_tools(request.extra)
                )
                if _disabled_tools_list:
                    _spawn_kwargs["disabled_tools"] = _disabled_tools_list
                # Parent-round pointer (V2 UX FIX): thread the round number
                # this sub-agent was dispatched at ONLY when the caller
                # knew its round (production main-loop path). Kept in the
                # optional ``_spawn_kwargs`` bag so light
                # ``SubAgentEventStreamPort`` test doubles that don't
                # declare ``parent_round_index`` still work — same policy
                # as ``allow_spawn`` / ``disabled_tools`` above.
                if round_index is not None:
                    _spawn_kwargs["parent_round_index"] = round_index
                # Alpha unified-spawn-path (main → sub-agent entry): the main
                # agent's dispatch is always the ROOT of the sub-agent tree,
                # so ``spawn_depth=1`` (a first-level sub-agent) and
                # ``parent_subagent_id=None`` (the direct parent IS the main
                # agent, which lives outside the sub-agent table). Threaded
                # via ``_spawn_kwargs`` so light ``SubAgentEventStreamPort``
                # test doubles that don't declare these kwargs still work —
                # same policy as ``allow_spawn`` / ``disabled_tools`` /
                # ``parent_round_index`` above. The reference implementation
                # (``AgentToolHandler``) accepts them.
                _spawn_kwargs["spawn_depth"] = 1
                _spawn_kwargs["parent_subagent_id"] = None
                # Fix 2 (immediate parent-Stop perception): thread the PARENT
                # tab's cooperative-abort predicate down into the sub-agent so
                # its round loop + per-round LLM stream guards observe the main
                # agent tab's ⏹ the instant it lands — not only via the Fix-1
                # owner cascade / task cancellation. ``handle.is_set`` reads the
                # SAME ``stream_abort_registry`` handle the main loop's kernel
                # ``abort_check`` uses (streaming.py:8966), so a Stop on this
                # tab flips both the parent AND every sub-agent it spawned.
                # Kept in the optional ``_spawn_kwargs`` bag so light
                # ``SubAgentEventStreamPort`` test doubles that don't declare
                # ``parent_abort_check`` still work — same policy as
                # ``spawn_depth`` / ``parent_subagent_id`` above.
                _spawn_kwargs["parent_abort_check"] = handle.is_set
                # Single-tool cancel (子 Agent 工具卡右上角停止按钮): thread the
                # PARENT tab's ``consume_cancel_tool`` down so the sub-agent can
                # honour a per-tool ``cancel_tool(tab_id, call_id)`` targeting one
                # of ITS OWN in-flight tool calls. The frontend marks the call on
                # the SAME ``handle`` (``request_cancel_tool`` → ``_cancel_tools``
                # set) the main loop polls (streaming.py dispatch); before this
                # the sub-agent never polled that set, so a sub-agent tool card's
                # stop returned ``cancelled=False`` forever (the reported bug).
                # ``consume_cancel_tool`` is a per-call, self-clearing check that
                # does NOT trip ``is_set`` — so it cancels exactly one tool while
                # the sub-agent turn continues (distinct from ``parent_abort_check``
                # = whole-turn Stop). Guarded ``getattr`` so a handle stub that
                # predates the capability degrades to no per-tool cancel (no
                # regression) instead of raising. Kept in the optional
                # ``_spawn_kwargs`` bag like ``parent_abort_check`` above so light
                # ``SubAgentEventStreamPort`` test doubles need not declare it.
                _parent_consume_cancel = getattr(
                    handle, "consume_cancel_tool", None
                )
                if _parent_consume_cancel is not None:
                    _spawn_kwargs["parent_consume_cancel_tool"] = (
                        _parent_consume_cancel
                    )
                async for event in self._agent_event_stream.iter_events(
                    inv_request,
                    agent_index=idx,
                    total_agents=total,
                    model_hint=request.model_hint,
                    # Forward the spawning turn's tool_mode so the sub-agent
                    # composes the SAME tool set as the main agent (incl. the
                    # conditional ``appbuilder_run`` for cloud app-builder turns).
                    tool_mode=(
                        (request.extra or {}).get("tool_mode")
                        or (request.extra or {}).get("_effective_tool_mode")
                    ),
                    workspace_root=self._effective_workspace_root_str(
                        _session_workspace_from_conv(conv),
                    ),
                    resume_session_id=resume_id,
                    subagent_type=subagent_type,
                    subagent_name=subagent_name,
                    **_spawn_kwargs,
                ):
                    await queue.put((idx, event))
                    etype = event.get("type")
                    if etype == "subagent_done":
                        final_results[idx] = str(event.get("result", ""))
                        sa_id = event.get("subagent_id")
                        if isinstance(sa_id, str) and sa_id:
                            final_subagent_ids[idx] = sa_id
                    elif etype == "subagent_error":
                        final_results.setdefault(
                            idx,
                            f"[sub-agent error: {event.get('message', 'unknown')}]",
                        )
            except Exception as exc:  # noqa: BLE001 — never propagate
                err_event = {
                    "type": "subagent_error",
                    "index": idx,
                    "message": str(exc),
                }
                await queue.put((idx, err_event))
                final_results[idx] = f"[sub-agent error: {exc}]"

        async def _run_all() -> None:
            await asyncio.gather(
                *(_drain_one(i, f) for i, f in enumerate(agent_tc_frames)),
                return_exceptions=False,
            )
            await queue.put(None)

        gather_task = asyncio.create_task(_run_all())

        # Blocked-read Stop failsafe + stall guard for sub-agent fan-out
        # (root-cause 3). The parent merges sub-agent events through ``queue``;
        # historically it did a bare ``await queue.get()`` and only checked
        # ``handle.is_set()`` AFTER a get returned. If any sub-agent's inner
        # (cloud) LLM stream hangs, ``_run_all``'s ``gather`` never completes,
        # the ``None`` all-done sentinel is never put, and ``queue.get()`` blocks
        # FOREVER — a user Stop cannot interrupt it (abort never puts anything on
        # the queue) and the parent ``agent`` tool card never gets its final
        # TOOL_RESULT (permanent "executing" spinner, the reported 50-min hang).
        # We instead poll the queue on a short window so we can (a) react to a
        # Stop promptly and (b) bound the worst-case wait if a sub-agent's
        # upstream silently stalls. ``_POLL`` keeps Stop feeling instant; the
        # stall budget is generous (sub-agents legitimately take a while) but
        # finite so a wedged upstream cannot pin the turn indefinitely.
        _POLL = 0.5
        _SUBAGENT_STALL_BUDGET_S = 600.0
        _since_last_event = 0.0
        try:
            while True:
                if handle.is_set():
                    break
                if gather_task.done() and queue.empty():
                    # All sub-agents finished and we've drained everything they
                    # produced; the ``None`` sentinel may not have been observed
                    # yet but there is nothing left to merge — stop cleanly.
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_POLL)
                except asyncio.TimeoutError:
                    _since_last_event += _POLL
                    if _since_last_event >= _SUBAGENT_STALL_BUDGET_S:
                        _log.warning(
                            "chat.streaming.subagent_merge_stalled",
                            stall_seconds=_since_last_event,
                            agents=total,
                        )
                        # Treat a wedged fan-out like an abort of this dispatch:
                        # the ``finally`` cancels ``gather_task`` and the per-idx
                        # ``final_results`` (filled with an error placeholder for
                        # any agent that never reported) still flow to the LLM so
                        # the turn can finish instead of hanging forever.
                        for _i in range(total):
                            final_results.setdefault(
                                _i,
                                "[sub-agent error: timed out waiting for "
                                "sub-agent output]",
                            )
                        break
                    continue
                _since_last_event = 0.0
                if item is None:
                    break
                idx, event = item
                etype = event.get("type")
                frame = _subagent_event_to_frame(
                    event, idx=idx, total=total, seq=seq,
                )
                if frame is None:
                    # Two distinct skip cases preserve the inline switch's
                    # exact sequence accounting:
                    #  * a recognised ``subagent_tool`` with an empty name
                    #    still advanced ``seq`` (the old ``seq += 1; continue``);
                    #  * an unknown ``type`` did NOT advance ``seq``.
                    if etype == "subagent_tool":
                        seq += 1
                    continue
                yield frame
                seq += 1
        finally:
            # Ensure the gather task is torn down even on abort / stall /
            # exception so we don't leak the background coroutines NOR re-block
            # on a wedged sub-agent. We left the merge loop either because:
            #  * the ``None`` sentinel arrived (gather already done — nothing to
            #    cancel);
            #  * the user aborted (``handle.is_set()``); or
            #  * a sub-agent's upstream stalled past the budget (root-cause 3).
            # In the latter two cases ``gather_task`` may still be running and
            # would never finish on its own, so we MUST cancel it rather than
            # ``await`` it (the old code only cancelled on ``handle.is_set()``,
            # which re-introduced the very hang on the stall path).
            if not gather_task.done():
                gather_task.cancel()
                try:
                    await gather_task
                except asyncio.CancelledError:
                    # Expected: we cancelled it above (abort or stall).
                    pass
                except Exception:  # noqa: BLE001 - best-effort drain
                    _log.warning(
                        "chat.streaming.gather_task_cleanup_failed",
                        exc_info=True,
                    )

        if handle.is_set():
            return

        # ── Emit the agent_summary marker + pipe consolidated results ──
        async for fr in self._emit_agent_summary_and_results(
            agent_tc_frames=agent_tc_frames,
            final_results=final_results,
            final_subagent_ids=final_subagent_ids,
            tool_results=tool_results,
            total=total,
            seq_start=seq,
        ):
            yield fr

    async def _emit_agent_summary_and_results(
        self,
        *,
        agent_tc_frames: list[StreamFrame],
        final_results: dict[int, str],
        final_subagent_ids: dict[int, str],
        tool_results: list[dict[str, Any]],
        total: int,
        seq_start: int,
    ) -> AsyncIterator[StreamFrame]:
        """Emit the ``agent_summary`` marker then the per-agent TOOL_RESULTs.

        Tail of :meth:`_dispatch_agent_calls_streaming` extracted for
        cohesion (B2).  Behaviour byte-for-byte identical to the inline
        version: one ``agent_summary`` frame (V1 chat_handler.py:694),
        then for each agent call one ``tool_results`` entry (mutated in
        place) plus one synthetic ``TOOL_RESULT`` frame so the parent
        loop's persistence sees the agent's result.  Frame ids /
        sequences / payloads are unchanged.

        V2 enhancement: when a sub-agent was persisted, the result text fed
        back to the main agent is prefixed with a ``subagent_id: <id>`` line
        (a resumable handle) so the LLM can re-pass it as the
        ``agent`` tool's ``resume_subagent_id`` to wake the SAME sub-agent for
        a related follow-up task. The resumable id reaches the UI / persistence
        via the ``subagent_done`` frame's ``subagent_id`` field and the
        ``meta.subAgentBlocks[].subagent_id`` block (the synthetic TOOL_RESULT
        frame itself carries only the result text).
        """
        seq = seq_start
        # ── Emit the agent_summary marker (V1 chat_handler.py:694) ──
        # The frontend uses this to insert a "main agent summary"
        # separator and reset its streaming-content buffer
        # (V1 useChat.js:1402-1408).
        yield StreamFrame.agent_summary(
            frame_id=f"sa-sum-{seq}",
            sequence=seq,
            total_agents=total,
        )
        seq += 1

        # ── Pipe consolidated text into the LLM's tool_results so the
        # parent agentic loop continues seamlessly.  Each agent call
        # also gets a synthetic TOOL_RESULT frame so persistence
        # (the outer ``_run`` loop's ``_tr_frames_for_persist``) sees
        # the agent's result alongside the SUBAGENT_* frames.  ──
        for idx, tc_frame in enumerate(agent_tc_frames):
            agent_text = final_results.get(idx, "")
            sub_id = final_subagent_ids.get(idx)
            args = tc_frame.payload.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            # Prefix the resumable handle so the main agent (LLM) can wake
            # this sub-agent later (a "task_id: ..." style lead line).
            # Only when persisted and not an error result. ``is_error`` matches
            # BOTH the top-level ``[sub-agent error: ...]`` and the per-round
            # ``[sub-agent round N error: ...]`` markers (fix: 🟡-3 — a sub-agent
            # that failed mid-round must report ok=False to the main agent and
            # not advertise itself as resumable).
            result_for_llm = agent_text
            is_error = agent_text.startswith(
                ("[sub-agent error:", "[sub-agent round ")
            )
            if sub_id and not is_error:
                result_for_llm = (
                    f"subagent_id: {sub_id} "
                    f"(reuse as resume_subagent_id to continue this sub-agent)\n"
                    f"{agent_text}"
                )
            tool_results.append(
                {
                    "tool_name": "agent",
                    "tool_call_id": tc_frame.payload.get("tool_call_id"),
                    "arguments": dict(args),
                    "result": result_for_llm,
                    "ok": not is_error,
                    "truncated": False,
                },
            )
            yield StreamFrame.tool_result(
                frame_id=f"sa-tr-{seq}",
                sequence=seq,
                tool_name="agent",
                result=result_for_llm,
                # Pair this synthetic result back to its TOOL_CALL frame by id,
                # exactly like ordinary tools (``_execute_single_tool_call`` /
                # ``_execute_tool_call_streaming``). Without it, the agent card's
                # result was ``unpaired_has_id`` at persist time → ``output`` left
                # None → ``rebuild_history_wire_messages`` dropped the whole
                # tool_call next turn (the "main agent forgets it dispatched a
                # sub-agent" amnesia bug). The id is the same one already used for
                # the ``tool_results`` list entry above (line ~9102).
                tool_call_id=tc_frame.payload.get("tool_call_id"),
            )
            seq += 1

    # --- legacy single-turn dispatch (PR-033) ---
    async def _dispatch_tool(
        self,
        *,
        tab_id: TabId,
        conversation_id: ConversationId,
        frame: StreamFrame,
    ) -> None:
        tool_name = frame.payload.get("tool_name")
        if not isinstance(tool_name, str):
            return
        arguments = frame.payload.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}
        await self._tools.invoke(
            ToolInvocationRequest(
                tab_id=tab_id,
                conversation_id=conversation_id,
                tool_name=tool_name,
                arguments=dict(arguments),
            ),
        )

    async def _capture_round_zero_snapshot(
        self,
        *,
        conv: Any,
        request: "StreamChatInput",
        llm_req: "LLMStreamRequest",
        round_request_ids: dict[int, str] | None,
    ) -> None:
        """Save the initial (round 0) prompt snapshot + record its id.

        Per-round snapshot (V1 parity): the initial LLM call is agentic
        round 0.  Saved here — right after ``_build_llm_request`` opened the
        first stream, so ``request.extra["system_prompt"]`` holds the
        fully-built prompt actually sent — rather than at turn end, so the
        round-0 tool cards carry round 0's own ``request_id`` (their 📄 opens
        the exact prompt the model saw on the FIRST call, distinct from later
        rounds).  ``conv.messages`` at this point is just the history +
        current user turn (no assistant/tool rounds appended yet), so the
        existing :meth:`_save_prompt_snapshot` reconstruction captures the
        round-0 prompt faithfully.  Best-effort + idempotent: skipped when no
        map is supplied or round 0 was already captured.  ``llm_req`` is
        accepted for symmetry / future accuracy hooks (the round-0 prompt is
        already faithfully reconstructed from ``conv.messages``).

        Storage note (shared-prefix review, problem 4): round 0 is kept as a
        STANDALONE full-copy snapshot rather than folded into the follow-up
        loop's shared ``turn_ref``.  The two paths build their wire shape
        differently — round 0 reconstructs prior-turn ``assistant{tool_calls}``
        + ``role:tool`` blocks from ``conv.messages`` here, whereas the
        follow-up rounds' :meth:`_build_base_wire_messages` replays history as
        plain ``{role, content}`` (no historical tool blocks) — so when the
        conversation already has tool-call history, round 0 is NOT a strict
        prefix of round 1 and sharing one ``turn_ref`` would break the
        prefix invariant.  Correctness wins: round 0 stays standalone (it
        costs only one extra short list per turn — the base history with no
        appended tool blocks — negligible vs. the O(N²) the shared follow-up
        rounds avoid).
        """
        if round_request_ids is None or 0 in round_request_ids:
            return
        rid = await self._save_prompt_snapshot(
            conv=conv, request=request, model_hint=request.model_hint,
        )
        if rid is not None:
            round_request_ids[0] = rid

    @staticmethod
    def _snapshot_enabled(request: "StreamChatInput") -> bool:
        """Return whether the prompt snapshot should be saved this turn.

        V1 parity (``backend/main.py:1360`` —
        ``if not show_prompt_in_ui: return None``): when the operator turned
        the ``service_launch.show_prompt_in_ui`` flag OFF the backend must NOT
        persist any prompt snapshot. The per-turn flag was stashed on
        ``request.extra`` by :meth:`_run`. Backward-compatible default: absent
        flag (no runtime-debug reader wired) → ``True`` so the prior
        always-save behaviour holds.
        """
        extra = request.extra if isinstance(request.extra, dict) else None
        if extra is None or "_show_prompt_in_ui" not in extra:
            return True
        return bool(extra.get("_show_prompt_in_ui"))

    async def _save_prompt_snapshot(
        self,
        *,
        conv: Any,
        request: "StreamChatInput",
        model_hint: str | None,
    ) -> str | None:
        """Persist a prompt snapshot for UI inspection (debug capture).

        Mirrors legacy ``backend/main.py:_save_prompt_snapshot`` —
        records the full messages list, model_id and tool_mode so users
        can inspect the exact prompt sent to the LLM.

        Returns the ``request_id`` UUID string on success so the caller
        can embed it in the terminal ``end`` frame (V1 parity:
        ``backend/main.py:6716-6720`` done frame payload contains
        ``request_id``).  Returns ``None`` when the snapshot store is
        not wired, the ``show_prompt_in_ui`` flag is off, or the save fails.
        """
        if self._prompt_snapshot_store is None:
            return None
        # V1 ``main.py:1360`` gate: skip when show_prompt_in_ui is off.
        if not self._snapshot_enabled(request):
            return None
        try:
            extra = request.extra or {}
            messages_payload: list[dict[str, Any]] = []
            # V1 parity (backend/main.py:_save_prompt_snapshot): the captured
            # message list starts with the SYSTEM prompt actually sent to the
            # model, so the dialog shows the full request (system + history).
            _system_prompt = self._resolve_snapshot_system_prompt(
                extra=extra, request=request,
            )
            if _system_prompt:
                messages_payload.append(
                    {"role": "system", "content": _system_prompt}
                )
            for msg in conv.messages:
                # V1 parity (backend/chat_handler.py:build_full_messages /
                # _sanitize_tool_messages): the snapshot must reflect the FULL
                # OpenAI wire shape sent to the model — not just the assistant
                # prose. A tool-call round persists its text in ``content`` but
                # its calls in the ``tool_calls`` field and the tool outputs in
                # each call's ``output`` (the ChatToolCall shape persisted by
                # build_tool_call_message). Previously this loop only copied
                # ``content.text``, so tool-call rounds showed up BLANK in the
                # dialog ("第一个历史提示词按钮内容不完整"). Reconstruct the
                # assistant{content, tool_calls} entry + a paired role:tool entry
                # per call so the dialog shows the complete request/response.
                text = (
                    msg.content.text
                    if hasattr(msg.content, "text") and msg.content.text
                    else ""
                )
                # SUBAGENT-RELOAD-PERSIST-INDEPENDENT-MSG (2026-07-02): the
                # ``subagent_summary`` message is a UI-only carrier for the
                # fold blocks (``meta.subAgentBlocks``) — the sub-agent's
                # actual output reached the model via the parent ``agent``
                # tool_call's TOOL_RESULT on the tool-call round's message.
                # Skip these rows so the prompt-snapshot dialog does not
                # show a phantom "[subagent_summary]" assistant utterance.
                if text == "[subagent_summary]":
                    continue
                # The placeholder body is an internal sentinel, never real text.
                if text == "[tool_calls]":
                    text = ""
                tool_calls = list(msg.tool_calls or ())
                d: dict[str, Any] = {"role": msg.role.value, "content": text}
                if tool_calls:
                    # OpenAI assistant.tool_calls wire shape (function call).
                    wire_calls: list[dict[str, Any]] = []
                    for tc in tool_calls:
                        if not isinstance(tc, dict):
                            continue
                        call_id = (
                            tc.get("id")
                            or tc.get("tool_call_id")
                            or f"call_{len(wire_calls)}"
                        )
                        args = tc.get("args")
                        wire_calls.append(
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": tc.get("tool", ""),
                                    "arguments": json.dumps(
                                        args if isinstance(args, dict) else {},
                                        ensure_ascii=False,
                                    ),
                                },
                            },
                        )
                    if wire_calls:
                        d["tool_calls"] = wire_calls
                messages_payload.append(d)
                # Emit a paired role:tool message per call carrying its output,
                # so the snapshot includes the tool RESULTS the model saw next
                # round (V1 keeps the role:tool messages in build_full_messages).
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    output = tc.get("output")
                    if output is None:
                        continue
                    messages_payload.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id")
                            or tc.get("tool_call_id")
                            or "",
                            "name": tc.get("tool", ""),
                            "content": output
                            if isinstance(output, str)
                            else json.dumps(output, ensure_ascii=False),
                        },
                    )
            return await self._persist_snapshot_payload(
                messages_payload=messages_payload,
                model_hint=model_hint,
                tool_mode=extra.get("tool_mode", ""),
                request_options=(
                    extra.get("_snapshot_request_options")
                    if isinstance(
                        extra.get("_snapshot_request_options"), dict
                    )
                    else None
                ),
            )
        except Exception as exc:  # noqa: BLE001 — best-effort debug capture
            _log.warning(
                "chat.prompt_snapshot_save_failed", error=str(exc),
            )
            return None

    def _resolve_snapshot_system_prompt(
        self,
        *,
        extra: dict[str, Any],
        request: "StreamChatInput",
    ) -> str | None:
        """Resolve the SYSTEM prompt to prepend to a snapshot's messages.

        Extracted from :meth:`_save_prompt_snapshot` so BOTH the round-0
        snapshot (built from ``conv.messages``) and the per-follow-up-round
        snapshot (:meth:`_save_round_snapshot`, built from the round's exact
        wire ``messages``) prepend the identical system prompt — V1 parity
        (``backend/main.py:_save_prompt_snapshot`` always captures
        system + history).  Prefer the already-built
        ``extra["system_prompt"]`` (set by :meth:`_build_llm_request`); else
        build it once via the system-prompt builder.  Returns ``None`` when
        no prompt is resolvable.
        """
        _sp_raw = extra.get("system_prompt")
        if isinstance(_sp_raw, str) and _sp_raw.strip():
            return _sp_raw

        # Rebuild fallback — only reached when ``_build_llm_request`` did
        # not run / could not persist the prompt onto ``extra`` (e.g. an
        # error path or a unit test driving ``_save_prompt_snapshot``
        # directly).  Local turns must reconstruct the SAME minimal prompt
        # the wire path produced — never the cloud builder's output (V1
        # parity bug fix: V1 ``build_full_messages`` always called the
        # cloud builder and so showed the wrong prompt for local turns;
        # see ``backend/chat_handler.py:1057-1075`` and the AGENTS task
        # description).
        if _is_local_model_hint(request.model_hint):
            try:
                return self._build_local_system_prompt(
                    extra=extra, request=request,
                )
            except Exception:  # noqa: BLE001 — best-effort capture
                return None

        if self._system_prompt_builder is None:
            return None
        try:
            _detect: dict[str, Any] = dict(extra)
            if "latest_user_message" not in _detect:
                _lm = getattr(request.user_message, "text", None)
                if isinstance(_lm, str) and _lm:
                    _detect["latest_user_message"] = _lm
            _sp = self._system_prompt_builder.build(
                SystemPromptRequest(
                    tool_mode=_detect.get("tool_mode"),
                    tool_params=_detect.get("tool_params"),
                    extra=_detect,
                ),
            )
            if isinstance(_sp.prompt, str) and _sp.prompt.strip():
                return _sp.prompt
        except Exception:  # noqa: BLE001 — best-effort capture
            return None
        return None

    def _build_snapshot_request_options(
        self,
        *,
        extra: dict[str, Any],
        request: "StreamChatInput",
    ) -> dict[str, Any]:
        """Capture the non-message request fields the adapter sends on wire.

        The snapshot's ``messages`` list already mirrors the system prompt +
        history + tool-call blocks, but the REAL request the model receives
        also carries the resolved tool schemas (``payload["tools"]``), the
        sampling parameters (temperature / top_p / max_tokens — resolved by
        :meth:`_apply_sampling_params` from the ModelParams panel + per-family
        locks) and, for on-device turns, a ``session_id``.  These never lived
        in the snapshot before, so the debug dialog could not show the user
        what tools / sampling values were actually advertised.

        Returns a plain dict (JSON-serialisable) describing those fields so
        :meth:`_persist_snapshot_payload` / :meth:`_save_round_snapshot` can
        attach it to the snapshot payload under ``request_options``.  Only
        keys with a real value are included (so the dialog shows exactly what
        was sent — no fabricated defaults, §State-Truth-First).
        """
        opts: dict[str, Any] = {}
        # Resolved tool schemas (OpenAI function-calling). BOTH cloud and local
        # turns send these as ``payload["tools"]`` (cloud → the provider; local
        # → the on-device daemon's PromptOptimizer). Neither path puts a
        # ``<tools>`` XML copy in the prompt body anymore (removed 2026-06-15).
        # Either way ``tools`` here describes the tools the model was given.
        tools = extra.get("tools_schemas")
        if isinstance(tools, (list, tuple)) and tools:
            opts["tools"] = [t for t in tools if isinstance(t, dict)]
            # Cloud function-calling always advertises ``tool_choice="auto"``
            # (llm_stream.py); local turns do not send it. Reflect the wire.
            if not _is_local_model_hint(request.model_hint):
                opts["tool_choice"] = "auto"
        # Sampling parameters (cloud only — local adapter does not send them).
        if not _is_local_model_hint(request.model_hint):
            sampling: dict[str, Any] = {}
            for key in (
                "temperature",
                "top_p",
                "max_tokens",
                "frequency_penalty",
                "presence_penalty",
                "seed",
                "stop",
            ):
                val = extra.get(key)
                if val is not None:
                    sampling[key] = val
            if sampling:
                opts["sampling"] = sampling
        else:
            # On-device payload forwards the conversation id as ``session_id``.
            conv_id = getattr(request, "conversation_id", None)
            sid = getattr(conv_id, "value", conv_id)
            if isinstance(sid, str) and sid:
                opts["session_id"] = sid
        return opts

    async def _persist_snapshot_payload(
        self,
        *,
        messages_payload: list[dict[str, Any]],
        model_hint: str | None,
        tool_mode: Any,
        request_options: dict[str, Any] | None = None,
    ) -> str | None:
        """Mint a ``request_id`` + persist one prompt snapshot.

        The terminal persistence step shared by the round-0
        :meth:`_save_prompt_snapshot` and the per-round
        :meth:`_save_round_snapshot`.  Builds the payload
        (``{model_id, tool_mode, messages, timestamp}`` — V1
        ``backend/main.py:_save_prompt_snapshot`` parity — plus the additive
        ``request_options`` describing the real wire fields: tools / sampling
        / session_id) and returns the freshly minted UUID so the caller can
        stamp it into that round's frames + persisted messages (so each tool
        card's 📄 opens ITS round's prompt).  Returns ``None`` when the store
        is not wired.
        """
        if self._prompt_snapshot_store is None:
            return None
        request_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "model_id": model_hint or "",
            "tool_mode": tool_mode if isinstance(tool_mode, str) else "",
            "messages": messages_payload,
            # V1 parity: the snapshot dialog reads ``data.timestamp`` to
            # show when the request was captured (ISO-8601, UTC).
            "timestamp": self._clock.now().isoformat(),
        }
        # Additive (v2.7 §3.1): surface the real non-message wire fields
        # (resolved tools / tool_choice / sampling params / session_id) so the
        # debug dialog can show the COMPLETE request, not just the messages.
        if request_options:
            payload["request_options"] = request_options
        await self._prompt_snapshot_store.save(
            PromptSnapshot(request_id=request_id, payload=payload),
        )
        return request_id

    async def _save_round_snapshot(
        self,
        *,
        wire_messages: list[dict[str, Any]],
        extra: dict[str, Any],
        request: "StreamChatInput",
        model_hint: str | None,
        turn_ref: str,
    ) -> str | None:
        """Persist ONE follow-up round's prompt snapshot (per-round, V1).

        V1 parity (``backend/main.py:_save_prompt_snapshot`` called once per
        ``/api/chat`` request = once per agentic round): each follow-up LLM
        round sends a DIFFERENT prompt (it accumulates the previous rounds'
        ``assistant{tool_calls}`` + ``role:tool`` blocks), so V1 stored a
        separate snapshot per round and each round's messages carried that
        round's ``request_id``.  This captures the EXACT ``wire_messages``
        the follow-up round is about to send (``extra["messages"]`` =
        :meth:`_build_base_wire_messages` + each completed round's
        :meth:`_append_tool_round` block), prepends the system prompt, and
        persists it — returning that round's own ``request_id``.

        Storage model (O(N) not O(N²)): each round's ``wire_messages`` is a
        strict PREFIX of the next round's, so instead of deep-copying the
        full growing history once per round we hand the store the current
        full list + ``turn_ref`` and let it keep ONE shared list per turn
        (the longest round) while this round records just a ``prefix_len``
        boundary (:meth:`PromptSnapshotStorePort.save_shared_prefix`).  A
        compression-driven rebuild passes a fresh ``turn_ref`` so a broken
        prefix never slices the wrong list.

        Best-effort: any failure logs + returns ``None`` (the round still
        streams).
        """
        if self._prompt_snapshot_store is None:
            return None
        # V1 ``main.py:1360`` gate: skip when show_prompt_in_ui is off.
        if not self._snapshot_enabled(request):
            return None
        try:
            messages_payload: list[dict[str, Any]] = []
            _system_prompt = self._resolve_snapshot_system_prompt(
                extra=extra, request=request,
            )
            if _system_prompt:
                messages_payload.append(
                    {"role": "system", "content": _system_prompt}
                )
            # ``wire_messages`` is already the OpenAI wire shape the adapter
            # sends verbatim (role/content + assistant.tool_calls + role:tool
            # blocks).  We pass the *live* entries by reference to the store;
            # the store keeps a single shared list per ``turn_ref`` and this
            # round only records its prefix boundary — so overlapping
            # prefixes across rounds cost zero extra copies (O(N) total).
            for entry in wire_messages:
                if isinstance(entry, dict):
                    messages_payload.append(entry)
            request_id = str(uuid.uuid4())
            # Reuse the turn's captured non-message wire fields (tools /
            # sampling / session_id). They were stored on ``request.extra``
            # by ``_build_llm_request`` once for the turn, so every round's
            # snapshot reflects the same advertised tools/sampling (additive).
            _req_extra = request.extra if isinstance(request.extra, dict) else {}
            _round_opts = _req_extra.get("_snapshot_request_options")
            await self._prompt_snapshot_store.save_shared_prefix(
                request_id=request_id,
                turn_ref=turn_ref,
                shared_messages=messages_payload,
                prefix_len=len(messages_payload),
                model_id=model_hint or "",
                tool_mode=(
                    extra.get("tool_mode", "")
                    if isinstance(extra.get("tool_mode", ""), str)
                    else ""
                ),
                timestamp=self._clock.now().isoformat(),
                request_options=(
                    _round_opts if isinstance(_round_opts, dict) else None
                ),
            )
            return request_id
        except Exception as exc:  # noqa: BLE001 — best-effort debug capture
            _log.warning(
                "chat.round_prompt_snapshot_save_failed", error=str(exc),
            )
            return None

    async def _publish(self, event: Any) -> None:
        if self._events is None:
            return
        # Per-frame streaming events NEVER go on the in-process event bus.
        #
        # The chat hot path calls ``_publish(ChatStreamFrameEvent(...))`` once
        # PER FRAME (per token). No production subscriber consumes per-frame
        # events: the chat tokens reach the front-end over the dedicated
        # ``/conversations/{id}/stream`` SSE (the route consumes the use case's
        # async iterator directly), channel mirroring consumes the use case
        # iterator via ``_chat_message_bridge``, and audit logging uses a PEP
        # 578 hook — none subscribe to the bus for tokens. The only bus
        # subscriber that ever MATCHED these was the global ``/api/events``
        # notification SSE (subscribes ``"*"``), which then DROPPED them again.
        # Forwarding per-frame events onto the bus only floods that bounded
        # queue (the historical ``events.backpressure`` log-spam). V1's chat
        # stream does not go through any in-process event bus at all — it yields
        # straight to the HTTP response. We align with that here: drop per-frame
        # events at the source so NO bus traffic is generated per token, and the
        # global notification channel needs no per-event-type denylist to stay
        # quiet. Lifecycle events (started / completed / aborted) are
        # low-frequency and still published normally below.
        if isinstance(event, ChatStreamFrameEvent):
            return
        # Best-effort: a domain-event notification must NEVER abort the user's
        # chat stream. If a slow/stale subscriber makes ``EventBus.publish``
        # raise, awaiting it here would surface as an ``error`` SSE frame and
        # CUT THE STREAM mid-answer. Treat event delivery as a best-effort
        # side-effect: log a warning and keep streaming. (Same contract as
        # conversation_management._publish_best_effort and
        # apps/api/_channel_webui_broadcast._publish.)
        try:
            await self._events.publish(event)
        except Exception as exc:  # noqa: BLE001 — notification must not break the stream
            _log.warning(
                "chat.stream_event_publish_failed",
                error=str(exc),
            )



# ---------------------------------------------------------------------------
# StopChat
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class StopChatInput:
    tab_id: TabId
    reason: str = "user_requested"


@dataclass(frozen=True, slots=True, kw_only=True)
class StopChatResult:
    """Outcome of :class:`StopChatUseCase`.

    ``aborted`` is ``False`` iff no in-flight stream was registered for
    the given tab; this is not an error -- a user may legitimately stop
    a stream that just finished on its own.
    """

    aborted: bool
    reason: str


class StopChatUseCase:
    """Signal an in-flight stream to abort cooperatively."""

    def __init__(
        self,
        *,
        abort_registry: StreamAbortRegistryPort,
        subagent_abort_registry: Any = None,
    ) -> None:
        self._abort_registry = abort_registry
        # Runtime-defect fix (the reported "主 Agent 停止后子 Agent 一直跑不停"
        # bug): a Stop on a tab used to signal ONLY the ``stream_abort_registry``
        # handle keyed by ``tab_id`` — an autonomously-spawned sub-agent's
        # cooperative-abort event (in the SEPARATE ``subagent_abort_registry``,
        # keyed by ``subagent_id``) was never set, so its round loop ran forever.
        # Injecting the sub-agent registry (additive, optional — ``None`` for
        # legacy / unit stubs, behaviour byte-for-byte unchanged) lets us
        # cascade the tab Stop into every in-flight sub-agent that tab spawned.
        # Duck-typed (``abort_by_owner_tab``) so tests can pass a light stub
        # without importing the concrete registry.
        self._subagent_abort_registry = subagent_abort_registry

    async def execute(self, request: StopChatInput) -> StopChatResult:
        ok = self._abort_registry.abort(request.tab_id, reason=request.reason)
        # Cascade: abort every in-flight sub-agent SPAWNED by this tab
        # (recorded via ``owner_tab_id`` at ``subagent_abort_registry.register``
        # time — see ``agent_tool._iter_loop``). Best-effort + defensive: a
        # missing registry (None) or a stub without the method is a no-op, and
        # a cascade failure must NEVER mask the primary tab abort. The sub-agent
        # loops poll their event between rounds and stop at the next round top.
        cascaded: tuple[str, ...] = ()
        registry = self._subagent_abort_registry
        if registry is not None:
            _abort_by_owner = getattr(registry, "abort_by_owner_tab", None)
            if callable(_abort_by_owner):
                try:
                    result = _abort_by_owner(
                        request.tab_id.value, reason=request.reason
                    )
                    cascaded = tuple(result) if result else ()
                except Exception as exc:  # noqa: BLE001 — cascade is best-effort
                    _log.warning(
                        "chat.stop_subagent_cascade_failed",
                        tab_id=request.tab_id.value,
                        error=str(exc),
                    )
        _log.info(
            "chat.stop_requested",
            tab_id=request.tab_id.value,
            aborted=ok,
            reason=request.reason,
            cascaded_subagents=len(cascaded),
            cascaded_subagent_ids=list(cascaded),
        )
        return StopChatResult(aborted=ok, reason=request.reason)


# ---------------------------------------------------------------------------
# CancelTool (per-call single-tool cancellation)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class CancelToolInput:
    tab_id: TabId
    call_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CancelToolResult:
    """Outcome of :class:`CancelToolUseCase`.

    ``cancelled`` is ``False`` iff no in-flight stream/handle was registered
    for the tab (or the handle predates the per-call capability); this is not
    an error — the tool may have finished on its own just before the click.
    """

    cancelled: bool
    call_id: str


class CancelToolUseCase:
    """Cancel ONE in-flight tool call WITHOUT tearing down the whole turn.

    Unlike :class:`StopChatUseCase` (which aborts the entire turn), this only
    records a per-call cancel request on the tab's abort handle. The tool
    dispatcher polls it, stops just that tool, synthesizes a ``[cancelled]``
    ``tool_result`` (fed back to the model), and lets the turn continue to the
    next round — so the model keeps working with the other tools' results.
    """

    def __init__(self, *, abort_registry: StreamAbortRegistryPort) -> None:
        self._abort_registry = abort_registry

    async def execute(self, request: CancelToolInput) -> CancelToolResult:
        cancel = getattr(self._abort_registry, "cancel_tool", None)
        ok = False
        if callable(cancel):
            try:
                ok = bool(cancel(request.tab_id, request.call_id))
            except Exception as exc:  # noqa: BLE001 — never break the caller
                _log.warning(
                    "chat.cancel_tool_failed",
                    tab_id=request.tab_id.value,
                    call_id=request.call_id,
                    error=str(exc),
                )
        _log.info(
            "chat.cancel_tool_requested",
            tab_id=request.tab_id.value,
            call_id=request.call_id,
            cancelled=ok,
        )
        return CancelToolResult(cancelled=ok, call_id=request.call_id)


__all__ = [
    "StreamChatUseCase",
    "StreamChatInput",
    "StopChatUseCase",
    "StopChatInput",
    "StopChatResult",
    "CancelToolUseCase",
    "CancelToolInput",
    "CancelToolResult",
    "AsyncioStreamAbortHandle",
    "DEFAULT_MAX_FOLLOWUP_ROUNDS",
    "LEGACY_MAX_FOLLOWUP_ROUNDS",
]
