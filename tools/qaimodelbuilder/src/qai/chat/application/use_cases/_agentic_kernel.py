# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Neutral agentic-kernel parts shared by the main + sub-agent loops.

Both the **main agent** loop (``streaming.py:_run_followup_loop`` + its
slices) and the **sub-agent** loop (``agent_tool.py:_iter_loop``) run the
SAME core algorithm: send the LLM one round → collect the round's
``tool_calls`` → execute them with strict ``tool_call_id`` pairing and a
*model-aware* per-result truncation → append the round's
``assistant{tool_calls}`` + paired ``role:tool`` blocks onto a growing
OpenAI wire history → optionally compress the wire history between rounds
using ONE shared threshold/ratio → cap the loop at ``MAX_ROUNDS`` rounds
and surface an INCOMPLETE/cap notice.

Historically those mechanics were re-implemented twice (the main loop in
``streaming.py`` and the sub-agent loop in ``agent_tool.py``), which let
them DRIFT: the sub-agent used a hard-coded 2 000-char per-tool cap while
the main loop used the model-aware :class:`ToolResultTruncatorPort`
(15k-50k); the sub-agent compressed at ratio ``0.75`` while the main loop
compressed at ``0.80``.  A bug fixed (or a limit tuned) in one loop did
NOT reach the other.

This module is the **single source of truth** for the parts that are
genuinely identical and SSE-/persistence-agnostic:

* shared limit constants (compression threshold / target ratio /
  preserve-tail / default max rounds);
* :func:`build_assistant_tool_calls_block` + :func:`build_tool_reply_blocks`
  — render one round's ``assistant.tool_calls`` array + paired
  ``role:tool`` replies with strict id pairing (replaces the two
  divergent copies);
* :func:`truncate_tool_result` — model-aware per-result truncation via the
  shared :class:`ToolResultTruncatorPort` (replaces the sub-agent's
  hard-coded 2 000-char cap so BOTH loops truncate identically and the cap
  is tunable in ONE place);
* :func:`maybe_compress_wire` — token-estimate-gated inter-round wire
  compression using the shared threshold/ratio (the sub-agent loop drives
  this directly; the main loop keeps its own ``conv``-based base-history
  compression integration but references the SAME constants here).

DESIGN — what the kernel parts DELIBERATELY do NOT do (kept in each
shell so the §3.1-locked wire stays byte-for-byte identical):

* no SSE ``StreamFrame`` stamping (``seq`` / ``round`` / ``request_id``);
* no DB persistence / per-round prompt snapshots / shared-prefix segments;
* no decision about WHERE the system prompt comes from (the main shell
  uses ``RichSystemPromptBuilder``; the sub-agent shell uses its own
  concise prompt — that difference is intentional and preserved);
* no knowledge of the ``agent`` tool (the recursion guard + parallel
  sub-agent dispatch live in the shells; the kernel only ever builds wire
  blocks + truncates + compresses).

Layering: this module lives in ``application/use_cases`` and depends only
on ``application.ports`` Protocols + ``domain`` types, so both the
application-layer main loop and the adapters-layer sub-agent loop may
import it without violating the ``layered-chat`` / ``context-isolation``
import-linter contracts.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from qai.chat.application.ports import (
    ContextCompressionPort,
    ToolResultTruncationRequest,
    ToolResultTruncatorPort,
)
from qai.chat.domain.model_profiles import get_context_limit
from qai.platform.logging import get_logger

__all__ = [
    "AGED_TOOL_OUTPUT_PLACEHOLDER",
    "AGING_MINIMUM_SAVINGS_TOKENS",
    "AGING_PROTECT_TOKENS",
    "COMPRESS_PRESERVE_TAIL",
    "COMPRESS_TARGET_RATIO",
    "COMPRESS_TARGET_WINDOW_RATIO",
    "DEFAULT_SUB_AGENT_MAX_ROUNDS",
    "INTER_ROUND_COMPRESS_THRESHOLD_RATIO",
    "PROTECT_WINDOW_RATIO",
    "SUBAGENT_SUMMARY_CONTENT_SENTINEL",
    "TOOL_CALLS_CONTENT_SENTINEL",
    "CompactionCheckpoint",
    "age_old_tool_outputs",
    "build_assistant_tool_calls_block",
    "build_cancelled_tool_message",
    "build_tool_reply_blocks",
    "estimate_wire_tokens",
    "is_self_contained_agent_hint",
    "maybe_compress_wire",
    "tool_result_already_truncated",
    "truncate_tool_result",
]

_log = get_logger(__name__)

# Internal content sentinel for an assistant turn that carried ONLY tool calls
# (no lead-in text). Stored as the wire ``content`` so the persisted turn is
# never empty, and recognised by the front-end mapper (`historyMapper` /
# `_wire_to_messages`) to render ONLY the tool cards (normalised to "" — no
# empty text bubble). Single source of truth for BOTH loops: the main loop
# (`_streaming_helpers`) and the sub-agent loop (`agent_tool`) MUST use this
# same value so a sub-agent's persisted/streamed turns are byte-identical to
# the main agent's (fixes the sub-agent's old hard-coded "Executing tools..."
# placeholder that leaked as a visible text bubble).
TOOL_CALLS_CONTENT_SENTINEL = "[tool_calls]"

# Sentinel for the DEDICATED sub-agent-blocks assistant message emitted by
# ``StreamChatUseCase._build_subagent_summary_message`` (SUBAGENT-RELOAD-
# PERSIST-INDEPENDENT-MSG, 2026-07-02). The message carries sub-agent fold
# blocks in ``meta.subAgentBlocks`` for UI-only rendering; its ``content.text``
# is a sentinel because :class:`MessageContent` forbids empty text.
#
# LLM-wire semantics: sub-agent runs' textual result is ALREADY delivered to
# the main agent via the parent ``agent`` tool_call's synthetic ``TOOL_RESULT``
# frame (whose ``output`` is persisted on the tool_call round's message).
# Re-emitting the fold blocks as an assistant message on the next turn would
# be duplicate context AND leak the sentinel string. Wire-rebuilders therefore
# **skip** persisted messages carrying this sentinel (see
# ``rebuild_history_wire_messages`` in ``_streaming_helpers``).
#
# Frontend historyMapper (``historyMapper.ts``) normalises the sentinel back
# to ``""`` on reload, alongside ``[tool_calls]``.
SUBAGENT_SUMMARY_CONTENT_SENTINEL = "[subagent_summary]"


# Reserved model-hint scheme for *query services* (internal-only). A hint like
# ``query::mb_pro`` routes the turn to a self-contained agent transport
# (``qai.chat.infrastructure.query_service`` — MB Pro / CEBot) via
# ``ProviderRoutingLLMStream``. Such an agent runs its OWN complete agentic loop
# server-side and streams back the final answer plus its ``tool_call`` /
# ``tool_result`` frames as DISPLAY cards (decision §2.8) followed by an explicit
# terminal END. It is NOT a bare LLM whose tool calls the host must execute and
# re-prompt. Single source of truth (this module is the neutral kernel both the
# main ``streaming.py`` loop and the discussion/sub-agent kernel import), so the
# follow-up / multi-round loop in EVERY shell can gate on it identically and
# finish the turn after the agent's own stream ends — otherwise each shell keeps
# re-prompting the agent every round (the "MB Pro 推理完成但界面一直忙碌 / 第N轮"
# stuck-busy bug, seen both in plain chat and in discussion mode).
_QUERY_SERVICE_HINT_PREFIX = "query::"


def is_self_contained_agent_hint(model_hint: str | None) -> bool:
    """Return True when ``model_hint`` targets a self-contained agent service.

    See ``_QUERY_SERVICE_HINT_PREFIX``. Used by every agentic shell to skip its
    local follow-up / multi-round tool loop for ``query::*`` turns: the agent
    already produced the final answer + display tool cards + a terminal END, so
    the shell must finish after the agent's stream ends instead of executing the
    agent's tools locally and re-prompting it (which never terminates).
    """
    return isinstance(model_hint, str) and model_hint.startswith(
        _QUERY_SERVICE_HINT_PREFIX
    )


def build_cancelled_tool_message(*, is_zh: bool) -> str:
    """The user- + model-facing text for a per-tool cancellation.

    SINGLE SOURCE OF TRUTH for the ``[cancelled]`` message shown on a tool
    card and fed back to the model when the user stops ONE tool (single-tool
    ``cancel_tool`` — NOT a whole-turn abort). Historically the main-agent
    (takeover) loop produced this bilingual text inline in
    ``streaming.py:_cancelled_tool_message`` while the sub-agent (main-派)
    loop produced a fixed English string (``_tool_round_executor`` /
    ``agent_tool``). Unifying it here lets BOTH engines emit the identical
    bilingual line so the cancellation reads the same whichever way the
    sub-agent was triggered (父子统一).

    Language follows the conversation via the caller's coarse CJK check on
    the user's text (mirrors ``intent_metrics.detect_language`` without an
    import) — good enough for a short status line; the model then continues
    the turn in whatever language it is already using.
    """
    if is_zh:
        return "[已取消] 用户已停止此工具的执行。"
    return "[cancelled] The user stopped this tool's execution."


# ---------------------------------------------------------------------------
# Shared limit constants (single source of truth for both loops)
# ---------------------------------------------------------------------------
# Inter-round compression SOFT threshold (fraction of the model's context
# window): when the running wire history is estimated to exceed this fraction
# of the budget, compress it before the next round.  The main loop's V1 SOFT
# threshold was ``0.80`` (streaming.py ``_PRESEND_COMPRESS_THRESHOLD``); the
# sub-agent loop had drifted to ``0.75``.  Unified here so tuning this one
# value re-tunes BOTH loops (user requirement: "以后根据需要统一调整").
INTER_ROUND_COMPRESS_THRESHOLD_RATIO: float = 0.80

# Target size ratio handed to ``ContextCompressionPort.compress`` — both
# loops pass ``0.5`` (main: ``_maybe_compress_round`` / ``_compress_history``;
# sub: ``_maybe_compress_wire``).  Unified.
COMPRESS_TARGET_RATIO: float = 0.5

# Number of most-recent messages the compressor preserves intact.  Compressing
# a history at or below this size is a no-op, so the caller may skip the probe
# entirely when ``len(wire_messages) <= COMPRESS_PRESERVE_TAIL``.
COMPRESS_PRESERVE_TAIL: int = 4

# Post-compression target as a fraction of the MODEL CONTEXT WINDOW (not the
# pre-compression size). The compressor brings the wire down to
# ``budget × COMPRESS_TARGET_WINDOW_RATIO`` — leaving headroom so the next turn
# does NOT immediately re-trigger compaction. This is the authoritative anchor
# for "how much to keep after compression" (window-anchored, unlike the legacy
# ``chars_before × ratio`` which is unrelated to the window). USER-CONFIGURABLE
# via the settings panel; this is only the default. 0.35 of a 200K window = 70K.
COMPRESS_TARGET_WINDOW_RATIO: float = 0.35

# Protection window as a fraction of the MODEL CONTEXT WINDOW: the most-recent
# complete turns whose cumulative size fits ``budget × PROTECT_WINDOW_RATIO``
# are preserved VERBATIM (never summarised / trimmed). The current turn (last
# user message and everything after it) is ALWAYS protected on top of this,
# regardless of budget. USER-CONFIGURABLE via the settings panel; this is only
# the default. ~0.35 of 200K = 70K of recent context kept intact.
PROTECT_WINDOW_RATIO: float = 0.35

# Default sub-agent round budget (the main loop's budget is configured
# separately via ``StreamChatUseCase`` / runtime settings — V1 parity).  The
# sub-agent's V1/v0.5 value is 15.
DEFAULT_SUB_AGENT_MAX_ROUNDS: int = 15

# ---------------------------------------------------------------------------
# Tool-output aging.
# A LIGHTWEIGHT complement to the 80%-threshold three-level compressor: it
# clears the CONTENT of OLD tool results (keeping the tool_call trace intact)
# so a long exploration turn does not accumulate every large tool_result in
# the wire until the 80% gate fires. Protects the most-recent tool outputs.
# ---------------------------------------------------------------------------
#: Cumulative token size of the most-recent tool outputs kept VERBATIM.
AGING_PROTECT_TOKENS: int = 40_000
#: Only age when the reclaimable old tool output totals at least this many
#: tokens (avoids churning on small savings).
#: 8K threshold lets accumulated OLD tool_results in exploration-heavy turns be
#: aged out earlier, reducing per-round wire bytes. On gateways that do NOT
#: return prompt-cache fields (e.g. cloud LLM service), this is a REAL per-round byte
#: saving cashed in every turn — cheaper than waiting for the ~20K reclaimable
#: mark. The protect window (AGING_PROTECT_TOKENS) is unchanged, so the most
#: recent tool outputs are still kept verbatim; only the "reclaim total >= N"
#: trigger drops from 20K to 8K.
AGING_MINIMUM_SAVINGS_TOKENS: int = 8_000
#: Placeholder that replaces an aged-out tool result's content, e.g.
#: "[Old tool result content cleared]".
AGED_TOOL_OUTPUT_PLACEHOLDER: str = "[Old tool result content cleared]"


# ---------------------------------------------------------------------------
# Session-level compaction checkpoint (in-memory, per conversation)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class CompactionCheckpoint:
    """A session-level, in-memory compaction checkpoint for ONE conversation.

    Records that "as of the original conversation's first ``anchor_index``
    messages, the compacted OpenAI wire is :attr:`compacted_wire`".  The wire
    sent to the model for a later turn is then assembled as::

        compacted_wire + rebuild_history_wire_messages(conv.messages[anchor:])

    i.e. the already-summarised head (NOT recompressed each turn) + the
    verbatim increment that accrued since the checkpoint was taken.

    DESIGN (user-approved "dual-track history" model):

    * ``conv.messages`` (the persisted aggregate) is NEVER mutated by
      compaction -- it stays the FULL original history for the user to read.
    * CCD-5 (PENDING-WORK.md §1): this checkpoint is now PERSISTED to sqlite
      (the ``chat_compaction_checkpoint`` table, one row per conversation),
      so it survives a process restart. The in-memory ``dict`` on
      :class:`StreamChatUseCase` is a WRITE-THROUGH fast path: every create /
      update writes through to
      :class:`~qai.chat.application.ports.CompactionCheckpointStorePort`, and
      the use case lazy-loads a conversation's checkpoint back from sqlite on
      its first turn after a restart. (Earlier this checkpoint lived ONLY in
      process memory and was lost on restart -- that "accept the loss" stance
      was superseded when CCD-5 made it durable.) It makes compaction
      **persistent + effective** for the model: in steady state only the
      increment past ``anchor_index`` is re-estimated, so a turn whose
      increment is still under the threshold reuses the prior summary instead
      of recompressing the whole history -- and that reuse now spans restarts.

    ``compacted_wire`` is a list of OpenAI wire dicts INCLUDING ``role:tool``
    outputs (the same shape :func:`maybe_compress_wire` /
    ``ContextCompressionPort.compress`` consume + return), so the three
    "wire"口径 — what is SENT to the LLM, what is ESTIMATED for the context
    badge, and what is fed to the COMPRESSOR — are one and the same.
    """

    anchor_index: int
    compacted_wire: list[dict[str, Any]] = field(default_factory=list)
    # Char-based estimate of the compacted-wire prompt-token size, computed at
    # checkpoint-creation time (cloud-first token accounting). Used as the
    # post-compaction figure ONLY until the next real turn runs — after which
    # the provider's measured ``usage.prompt_tokens`` on the latest assistant
    # turn is the authoritative compacted-wire size and is preferred. ``None``
    # when no estimate was stashed. Optional appended field (tail-append).
    estimated_tokens: int | None = None
    # TPP-1 post-compaction delta growth: the last cloud-measured effective
    # prompt-token size (``prompt_tokens`` + cache_read for the Anthropic
    # family) observed on a post-compaction assistant turn. Seeded ``None`` at
    # checkpoint creation; set on the FIRST post-compaction finalize (so that
    # turn contributes no delta — the char ``est``口径 differs from the cloud
    # measurement), then each subsequent turn grows ``conv.full_history_tokens``
    # by the positive delta against this baseline. Optional appended field
    # (tail-append).
    last_eff_prompt: int | None = None
    # CCD-1 (PENDING-WORK.md §1): wall-clock seconds when this checkpoint was
    # created. Diagnostic only (logging + future "stale checkpoint" guards);
    # NOT used as a primary discriminator (id-based ``anchor_message_id`` is
    # the authoritative "before/after compaction" marker — created_at can be
    # ambiguous across clock skew / restore-from-disk in a future persistence
    # PR). Optional appended field (tail-append, AGENTS.md §3.1).
    created_at: float = field(default_factory=time.time)
    # CCD-1 (PENDING-WORK.md §1): id of ``conv.messages[anchor_index-1]``
    # (i.e. the LAST message inside the compacted head) at checkpoint
    # creation time, or ``None`` when ``anchor_index == 0`` (whole-history
    # compaction — every message is "after" the anchor). ``estimate_compacted_tokens``
    # passes this to ``_last_assistant_with_usage(after_message_id=...)`` so
    # the badge's post-compaction figure considers ONLY post-compaction
    # assistant usage, never the (much larger) pre-compaction assistant's
    # ``last_round_prompt_tokens`` that would otherwise still be there in
    # the brief window between checkpoint creation and the first post-
    # compaction usage block landing. Optional appended field (tail-append).
    anchor_message_id: str | None = None


def estimate_wire_tokens(
    wire_messages: list[dict[str, Any]],
    *,
    model_hint: str | None,
) -> int:
    """Estimate the prompt-token count of a full OpenAI wire list.

    Single口径 used by every main-loop compaction TRIGGER.  This is an
    intentionally COARSE chars/4 heuristic over every message's ``content``
    AND its ``tool_calls`` arguments — it is the gate for the side-path
    compaction DECISION only, NOT the user-facing /context number (that uses
    the provider-measured cloud running counter ``conv.full_history_tokens``).

    No BPE / tiktoken is used here on purpose: the compaction threshold carries
    20% headroom and the PROMPT_TOO_LONG retry is the safety net for any
    trigger miss, so a cheap, BPE-free estimate is sufficient and keeps the hot
    loop free of tokenizer CPU.
    """
    _ = model_hint  # retained for signature stability / call-site clarity
    total_chars = 0
    for m in wire_messages:
        total_chars += len(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or ():
            if isinstance(tc, dict):
                fn = tc.get("function")
                if isinstance(fn, dict):
                    total_chars += len(str(fn.get("arguments") or ""))
    return total_chars // 4


# ---------------------------------------------------------------------------
# Wire-history block builders (strict id pairing) — shared by both loops
# ---------------------------------------------------------------------------
# These render the OpenAI wire shape both loops grow round by round:
# ``assistant{content, tool_calls:[{id,type,function}]}`` immediately followed
# by one ``tool{tool_call_id}`` per call, paired by id.  The ``id`` carried on
# ``assistant.tool_calls[i].id`` MUST equal the ``role:tool.tool_call_id`` of
# the matching reply, or ``sanitize_tool_messages`` drops the orphan and the
# model loses the executed tool RESULT (the historical "sub-agent answers the
# wrong question" bug — see agent_tool.py module docstring).
#
# V1 parity: ``json.dumps(..., ensure_ascii=False)`` keeps non-ASCII argument
# values (e.g. Chinese paths / commands like ``上海``) verbatim on the wire
# (chat_handler.py:784 / streaming.py:2595 / agent_tool.py:188).


def build_assistant_tool_calls_block(
    tool_metas: list[tuple[str, dict[str, Any], str]],
    *,
    thought_signatures: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Render the OpenAI ``assistant.tool_calls`` array for one round.

    ``tool_metas`` is a list of ``(tool_name, arguments, call_id)`` in the
    round's original call order.  ``call_id`` is the model-emitted id when
    present, else a caller-synthesised round-unique id (never a bare
    ``call_{i}`` that would collide across rounds).

    ``thought_signatures`` optionally maps ``call_id -> signature`` so the
    main loop can re-attach the Vertex AI ``thought_signature`` it stashed on
    the executed result dict (PR-090 C-1; chat_handler.py:787-789).  Absent
    for the sub-agent / non-Vertex providers.
    """
    block: list[dict[str, Any]] = []
    for tool_name, arguments, call_id in tool_metas:
        try:
            args_str = json.dumps(
                arguments if isinstance(arguments, dict) else {},
                ensure_ascii=False,
            )
        except (TypeError, ValueError):
            args_str = "{}"
        entry: dict[str, Any] = {
            "id": call_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": args_str},
        }
        if thought_signatures is not None:
            sig = thought_signatures.get(call_id)
            if sig:
                entry["thought_signature"] = sig
        block.append(entry)
    return block


def build_tool_reply_blocks(
    tool_metas: list[tuple[str, dict[str, Any], str]],
    tool_results: list[Any],
    *,
    include_name: bool = False,
    durations_ms: list[int | None] | None = None,
) -> list[dict[str, Any]]:
    """Render one ``role:tool`` reply per call IN ORIGINAL ORDER.

    Each reply carries the ``tool_call_id`` that matches its
    ``assistant.tool_calls[i].id`` so ``sanitize_tool_messages``
    (message_sanitizer.py:241-250) never treats it as an orphan and drops it.
    A failed tool (a :class:`BaseException`, e.g. from
    ``asyncio.gather(return_exceptions=True)``) surfaces as a
    ``[tool_error] ...`` string (V1 chat_handler.py:2315-2316).

    ``tool_results`` pairs 1:1 (and in the same order) with ``tool_metas``;
    a length mismatch is a programming error and raises (``strict=True``).
    Each result is expected to already be the final, possibly-truncated
    string (callers truncate via :func:`truncate_tool_result` before this).

    ``include_name`` controls whether each ``role:tool`` message carries an
    OpenAI ``name`` field naming the tool.  The sub-agent loop has always
    included it (``True``); the main loop's prior ``_append_tool_round`` did
    NOT, so it passes ``False`` to keep its outbound LLM payload byte-for-byte
    identical.  Both shapes are valid OpenAI ``role:tool`` messages; the flag
    only preserves each caller's exact pre-unification wire bytes.

    ``durations_ms`` (optional) pairs 1:1 with ``tool_metas`` and carries each
    call's wall-clock execution time (ms).  When present it is stamped as a
    DISPLAY-ONLY ``duration_ms`` field on the persisted ``role:tool`` block so
    a reloaded history card (e.g. a sub-agent opened in its own tab via
    ``_wire_to_messages``) shows "took N ms" — the SAME enhancement the main
    agent persists on its message ``tool_calls`` (``_streaming_helpers.py``).
    It is a non-standard key, so callers MUST strip it before the wire is sent
    to the model (sub-agent loop adds it to ``build_send_wire``'s
    ``_display_only`` set, exactly like ``created_at`` / ``request_id``).
    """
    out: list[dict[str, Any]] = []
    for idx, ((tool_name, _arguments, call_id), tool_result) in enumerate(
        zip(tool_metas, tool_results, strict=True)
    ):
        if isinstance(tool_result, BaseException):
            result_text = f"[tool_error] {tool_result}"
        else:
            result_text = str(tool_result)
        block: dict[str, Any] = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": result_text,
        }
        if include_name:
            block["name"] = tool_name
        if durations_ms is not None and idx < len(durations_ms):
            dur = durations_ms[idx]
            if dur is not None:
                block["duration_ms"] = dur
        out.append(block)
    return out


# ---------------------------------------------------------------------------
# Model-aware per-result truncation (shared port — replaces hard-coded caps)
# ---------------------------------------------------------------------------
def tool_result_already_truncated(raw_result: Any) -> bool:
    """Return ``True`` when a tool MANAGES ITS OWN output size.

    The robust, tool-agnostic signal used by BOTH agentic loops to decide
    whether the second-layer head+tail truncator should skip a result. A tool
    that ships a ``truncated`` key in its result dict (whether ``True`` or
    ``False``) has its OWN size/recovery contract: when it bounds output it
    sets ``truncated=True`` and appends a recovery footer
    (``read(path=...)`` / ``offset=N`` / "use a tighter pattern"); when it
    returns the whole result it sets ``truncated=False`` and there is nothing
    to recover. In BOTH cases the second-layer head+tail split is wrong:

    * ``truncated=True`` — re-splitting corrupts the tool's own recovery
      footer and drops the middle a second time;
    * ``truncated=False`` — the result is WHOLE but may still exceed the
      per-family char budget (e.g. a 68KB ``read`` slice, a 40KB ``list``
      page, a 35KB ``grep`` result, a large ``webfetch`` body). Splitting it
      would drop the middle of an ordered/whole result that the tool
      deliberately returned intact, with NO recovery path (these tools recover
      by RE-INVOKING with a narrower scope, not by reading a persisted file).

    So the rule is: **if the tool self-reports a ``truncated`` flag at all, it
    owns truncation — the second layer keeps hands off.** This mirrors the
    "tool already set its truncated metadata → skip the generic truncator"
    design and replaces the earlier "only skip when truncated is truthy", which
    let whole-but-large ``read`` / ``list`` / ``grep`` / ``webfetch`` results
    slip through and get head+tail split.

    Accepts the raw tool result before it is flattened to text:

    * a ``dict`` envelope — skipped when it CONTAINS a ``truncated`` key or a
      ``stored_path`` (the persisted-output retrieval path);
    * anything else (already a string, an exception, ``None``) — ``False`` so
      the head+tail backstop still applies to unstructured results.
    """
    if isinstance(raw_result, dict):
        if "truncated" in raw_result:
            return True
        if raw_result.get("stored_path"):
            return True
    return False


def truncate_tool_result(
    truncator: ToolResultTruncatorPort,
    *,
    model_hint: str | None,
    tool_name: str,
    result_text: str,
    already_truncated: bool = False,
) -> tuple[str, bool, int]:
    """Truncate one tool result via the SHARED model-aware truncator.

    Returns ``(text, truncated, original_length)``.  Both the main loop and
    the sub-agent loop call this so a single oversized output is sliced to the
    SAME model-aware per-family budget (Claude/Gemini 50k, mid 50k, low 30k;
    see :class:`AdaptiveToolResultTruncator`) BEFORE it enters the wire
    history.

    ``already_truncated`` is forwarded to the truncator: when the producing
    tool STRUCTURALLY reported that it bounded its own output (``truncated``
    flag and/or a persisted ``stored_path`` on its result dict), the truncator
    passes the text through unchanged so the tool's own recovery footer
    (``read(path=...)`` / ``offset=N``) is never head+tail split. The robust,
    tool-agnostic skip replaces the earlier footer-substring matching, which
    only recognised one specific marker and let glob / grep / list footers
    slip through.

    The oversized-output STORE (``data/tool_results/`` + ``read`` retrieval)
    is the robust replacement for "one tool result must not blow the budget".

    The cap is tunable in ONE place (the injected
    :class:`ToolResultTruncatorPort`) for BOTH loops (user requirement).
    """
    result = truncator.truncate(
        ToolResultTruncationRequest(
            model_id=model_hint or "",
            tool_name=tool_name,
            result_text=result_text,
            already_truncated=already_truncated,
        ),
    )
    return result.text, result.truncated, result.original_length


# ---------------------------------------------------------------------------
# Tool-output aging (lightweight, complements the three-level compressor)
# ---------------------------------------------------------------------------
def age_old_tool_outputs(
    wire_messages: list[dict[str, Any]],
    *,
    model_hint: str | None = None,
    protect_tokens: int = AGING_PROTECT_TOKENS,
    minimum_savings_tokens: int = AGING_MINIMUM_SAVINGS_TOKENS,
    keep_recent_tool_results: int | None = None,
    min_aged_tool_count: int | None = None,
) -> list[dict[str, Any]]:
    """Clear the content of OLD tool-result messages, keeping recent ones.

    Walks the wire from NEWEST to OLDEST accumulating tool-output token
    estimates. Tool results within the most-recent ``protect_tokens`` budget
    are kept verbatim; older ones are queued for aging. Only when the queued
    (reclaimable) total reaches ``minimum_savings_tokens`` do we actually
    replace their content with ``AGED_TOOL_OUTPUT_PLACEHOLDER`` (the tool_call
    trace / role / tool_call_id are preserved so the causal chain stays
    intact). Already-aged messages are skipped. Returns a NEW list; input is
    not mutated. Best-effort.

    ``keep_recent_tool_results`` is an OPTIONAL count-based cap layered on top
    of the token-window logic. When ``None`` (the DEFAULT — used by the main
    agent) the behaviour is byte-for-byte identical to the token-window-only
    logic above; the count path is entirely inert. When set to an integer N
    (used by the sub-agent, which fans out many exploration tool calls), the
    walk additionally counts ``role:tool`` messages newest→oldest and queues
    for aging every tool result BEYOND the most-recent N — even if it still
    sits inside the ``protect_tokens`` window. The most-recent N tool results
    are ALWAYS kept verbatim (a sub-agent must never lose the content it just
    read, which would force a costly re-read). The ``minimum_savings_tokens``
    churn-gate still applies: even count-selected messages are only cleared
    when the reclaimable total crosses the gate, so a small wire is left
    untouched.

    ``min_aged_tool_count`` is an OPTIONAL lower-bound that MONOTONICALLY
    stabilises the aged prefix for Anthropic prompt-caching (改动2b). When
    ``None`` (the DEFAULT — the main agent's current call site) it is entirely
    inert and the behaviour is byte-for-byte the token-window logic above. When
    set to an integer N, the OLDEST N ``role:tool`` messages are FORCE-AGED
    (added to the queue) even if they still sit inside the ``protect_tokens``
    window — because the OLDEST results are the ones the model is least likely
    to still need, and freezing them into placeholders makes the low prefix
    byte-identical across rounds so an Anthropic ``cache_control`` breakpoint
    placed just after that region actually HITS. This is orthogonal to
    ``keep_recent_tool_results`` (which protects the NEWEST N): the oldest-N
    force-age and the newest-N protect never overlap unless the caller sets
    N_old + N_new > total tools, in which case the newest-protect wins for any
    message that is both (a message can only be aged once). The
    ``minimum_savings_tokens`` churn-gate still applies uniformly.
    """
    # Per-message token estimate reuses the SAME chars/4口径 as
    # ``estimate_wire_tokens`` (content + tool_calls args) by wrapping the
    # single message in a one-element list — one authoritative estimator, no
    # drift between the aging gate and the compaction trigger.
    def _msg_tokens(msg: dict[str, Any]) -> int:
        return estimate_wire_tokens([msg], model_hint=model_hint)

    # Pre-count NOT-yet-aged ``role:tool`` messages so the oldest-N force-age
    # (改动2b monotonic boundary) can identify "the oldest N" during the
    # newest→oldest walk: a message is among the oldest N iff its
    # newest→oldest ordinal (``tool_seen``) is > ``not_aged_total - N``.
    # ``None`` boundary → this whole path is inert (main-agent default).
    not_aged_total = 0
    if min_aged_tool_count is not None and min_aged_tool_count > 0:
        for _m in wire_messages:
            if _m.get("role") != "tool":
                continue
            _c = _m.get("content")
            if isinstance(_c, str) and _c == AGED_TOOL_OUTPUT_PLACEHOLDER:
                continue
            not_aged_total += 1

    # Walk newest → oldest. Accumulate protected tool tokens; queue indices of
    # older, not-yet-aged tool messages together with their reclaimable size.
    protected = 0
    to_age: list[int] = []
    reclaimable = 0
    tool_seen = 0  # count of role:tool messages encountered newest→oldest
    oldest_forced_any = False  # any oldest-N force-age queued (bypasses gate)
    for idx in range(len(wire_messages) - 1, -1, -1):
        msg = wire_messages[idx]
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        # Already aged (content == placeholder) → skip so we never re-process
        # nor double-count something we already cleared (stop
        # when you hit an already-compacted entry — here: per-message
        # idempotence).
        if isinstance(content, str) and content == AGED_TOOL_OUTPUT_PLACEHOLDER:
            continue
        tool_seen += 1
        tokens = _msg_tokens(msg)
        # Count-based cap (sub-agent path): once we have already kept the
        # most-recent ``keep_recent_tool_results`` tool results, everything
        # OLDER is a candidate for aging regardless of the token window. The
        # first N (tool_seen <= N) are ALWAYS protected verbatim.
        count_forces_age = (
            keep_recent_tool_results is not None
            and tool_seen > keep_recent_tool_results
        )
        # Oldest-N force-age (改动2b monotonic boundary): the OLDEST
        # ``min_aged_tool_count`` tool results are queued for aging even if
        # they sit inside the protect window, so the low prefix freezes and an
        # Anthropic cache breakpoint just after it hits. A message is among the
        # oldest N iff its newest→oldest ordinal exceeds ``not_aged_total - N``.
        oldest_forces_age = (
            min_aged_tool_count is not None
            and min_aged_tool_count > 0
            and tool_seen > not_aged_total - min_aged_tool_count
        )
        if (
            not count_forces_age
            and not oldest_forces_age
            and protected + tokens <= protect_tokens
        ):
            # Within the most-recent protection budget → keep verbatim.
            protected += tokens
            continue
        # Older than the protection window (or beyond the count cap, or among
        # the oldest-N force-age set) → candidate for aging.
        to_age.append(idx)
        reclaimable += tokens
        if oldest_forces_age:
            oldest_forced_any = True

    if reclaimable < minimum_savings_tokens and not oldest_forced_any:
        # Not worth churning — return the input list unchanged (identity).
        # EXCEPTION: when the oldest-N force-age (改动2b monotonic boundary)
        # queued at least one message we DO age even below the savings gate —
        # the goal there is a byte-stable low prefix for Anthropic caching, not
        # token savings, so the churn-gate must not suppress it (otherwise the
        # boundary would silently fail to advance and the cache would keep
        # missing). The main-agent default (``min_aged_tool_count is None``)
        # never sets ``oldest_forced_any`` → gate behaviour byte-for-byte
        # unchanged.
        return wire_messages

    # Build a NEW list; only the queued tool messages get a new dict with the
    # content replaced. Every other message object is passed through by
    # reference (input is never mutated).
    to_age_set = set(to_age)
    aged: list[dict[str, Any]] = []
    for idx, msg in enumerate(wire_messages):
        if idx in to_age_set:
            aged.append({**msg, "content": AGED_TOOL_OUTPUT_PLACEHOLDER})
        else:
            aged.append(msg)

    _log.info(
        "chat.agentic_kernel.aging.done",
        aged_messages=len(to_age),
        reclaimable_tokens=reclaimable,
        protected_tokens=protected,
        protect_budget=protect_tokens,
    )
    return aged


# ---------------------------------------------------------------------------
# Inter-round wire compression (shared threshold/ratio) — drives the sub-agent
# loop directly; the main loop references the SAME constants from its own
# ``conv``-based base-history compression integration.
# ---------------------------------------------------------------------------
async def maybe_compress_wire(
    wire_messages: list[dict[str, Any]],
    *,
    compressor: ContextCompressionPort | None,
    model_hint: str | None,
    threshold_ratio: float = INTER_ROUND_COMPRESS_THRESHOLD_RATIO,
    target_ratio: float = COMPRESS_TARGET_RATIO,
    preserve_tail: int = COMPRESS_PRESERVE_TAIL,
    log_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Compress the running wire history when it nears the context budget.

    No-op when the compressor is unwired, the history is trivially small
    (``<= preserve_tail``), or the token estimate is under
    ``threshold_ratio * context_budget``.  Best-effort: any failure returns
    the input unchanged so a compression hiccup never aborts a turn.

    Reuses the SAME :class:`ContextCompressionPort` three-level pipeline both
    loops share (tool-output prune → LLM summary → hard trim), applied to a
    wire dict array.  The threshold/ratio default to the shared constants so
    tuning them re-tunes every caller.
    """
    if compressor is None or len(wire_messages) <= preserve_tail:
        return wire_messages

    model_id = (model_hint or "").removeprefix("local::") or "__unknown__"
    budget = get_context_limit(model_id)
    threshold = int(budget * threshold_ratio)

    # Unified口径 with the main-loop trigger: a BPE-free chars/4 estimate over
    # content + tool_calls args (see :func:`estimate_wire_tokens`). The gate is
    # intentionally BPE-free; the PROMPT_TOO_LONG retry is the safety net.
    used = estimate_wire_tokens(wire_messages, model_hint=model_hint)

    if used < threshold:
        return wire_messages

    _ctx = dict(log_context or {})
    _log.info(
        "chat.agentic_kernel.compress.trigger",
        model_id=model_id,
        used_tokens=used,
        budget=budget,
        threshold=threshold,
        messages_before=len(wire_messages),
        **_ctx,
    )
    try:
        # NOTE: ``used`` here is a BPE-free chars/4 ESTIMATE, not a real
        # provider measurement, so we do NOT pass it as ``wire_actual_tokens``
        # (that would dress an estimate up as a measurement and just collapse
        # to the same fallback density). Passing ``None`` lets the compressor
        # use its fallback chars/token factor honestly. The main loop
        # (streaming.py) DOES have the provider-measured size and injects it.
        compressed = await compressor.compress(
            wire_messages,
            target_ratio=target_ratio,
            preserve_tail=preserve_tail,
            budget_tokens=budget,
            protect_ratio=PROTECT_WINDOW_RATIO,
            wire_actual_tokens=None,
            model_hint=model_hint,
        )
    except Exception as exc:  # noqa: BLE001 — compression is best-effort
        _log.warning(
            "chat.agentic_kernel.compress.failed",
            error=str(exc),
            error_type=type(exc).__name__,
            **_ctx,
        )
        return wire_messages
    _log.info(
        "chat.agentic_kernel.compress.done",
        messages_after=len(compressed),
        **_ctx,
    )
    return compressed
