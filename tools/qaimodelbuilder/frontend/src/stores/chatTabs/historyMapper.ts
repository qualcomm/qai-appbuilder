// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * History-message mapping for the chat store (cohesion split, ARCH-1).
 *
 * Maps raw `MessageItem` rows from `GET …/messages` into `ChatMessage`.
 * Shared by `loadHistoryMessages` (newest page) and `loadMoreMessages`
 * (older pages). Moved verbatim from `chatTabs.ts` — this is a pure
 * function with no Pinia / reactive dependency, so it lives outside the
 * store body.
 *
 * The V2 messages wire schema carries
 * `{id, role, text, created_at, parent_id, tool_calls?, usage?, meta?}`.
 * Token `usage` (P1-4) is persisted server-side and re-emitted here so the
 * per-message token line survives a page reload. The V1-parity `meta`
 * envelope (P1: persisted via Message.meta + migration 021) carries the
 * remaining render extras the live stream produced — `request_id` (prompt
 * snapshot button), `perf` (perf line: ttft_ms / total_ms / token counts),
 * `subAgentBlocks` (sub-agent fold blocks) — so they are rehydrated here too,
 * matching V1 (`backend/history_store.py:_row_to_message` promoted the same
 * fields out of `messages.meta`). Image previews need no rehydration: the
 * upload URL lives inside the persisted message `content` markdown
 * (`![name](/api/images/…)`) and `ChatMessageList.extractImages` re-parses
 * it on render. Tool-truncation badges / full-output tabs rehydrate via the
 * `tool_calls[]` entries' `output` / `outputSize` / `truncated` fields.
 */
import type {
  ChatMessage,
  ChatMessageRole,
  ChatMessagePerf,
  ChatMessageUsage,
  ChatToolCall,
  SubAgentBlock,
} from "../_chatTabsTypes";

/** Wire shape of a single row returned by `GET …/messages`. Shared by
 *  both history-load actions so the inline literal is not duplicated. */
export interface HistoryMessageItem {
  id: string;
  role: string;
  text: string;
  created_at: string;
  parent_id: string | null;
  tool_calls?: ChatToolCall[] | null;
  usage?: ChatMessageUsage | null;
  model_id?: string | null;
  model_provider?: string | null;
  meta?: Record<string, unknown> | null;
  /** Discussion participant id that authored an assistant turn (V2 multi-agent).
   *  Used after participants re-hydration to restore the speaker's role name +
   *  avatar colour on a reloaded history bubble. Absent for single-agent rows. */
  sender_id?: string | null;
}

/** Wire shape of a `GET …/messages` page response. */
export interface HistoryMessagesPage {
  items: HistoryMessageItem[];
  next_cursor: string | null;
}

/** Roles the history endpoint may return; anything else is coerced to
 *  `assistant` for safe rendering (V1 treated unknown roles as model
 *  output). */
const _HISTORY_ALLOWED_ROLES: ReadonlySet<string> = new Set([
  "user",
  "assistant",
  "system",
  "tool",
  "tool_indicator",
]);

/**
 * Rehydrate derived perf fields that are computed client-side during live
 * streaming (V1 useChat.js:2377-2389) but not persisted by the backend.
 *
 * The backend persists the raw timing/token data:
 *   `{ttft_ms, total_ms, input_tokens?, output_tokens?, tool_rounds?}`
 *
 * The frontend derives from those:
 *   - `input_tps`  = input_tokens / (ttft_ms / 1000)  [prompt processing rate]
 *   - `output_tps` = output_tokens / ((total_ms - ttft_ms) / 1000) [gen rate]
 *   - `tool_rounds` — persisted when backend passes it; otherwise inferred
 *     from presence of tool_calls on the message (V1 parity fallback).
 */
function _rehydratePerf(
  raw: Record<string, unknown>,
  toolCallCount: number,
  usage: ChatMessageUsage | null | undefined,
): ChatMessagePerf {
  const ttft_ms =
    typeof raw.ttft_ms === "number" ? (raw.ttft_ms as number) : undefined;
  const total_ms =
    typeof raw.total_ms === "number" ? (raw.total_ms as number) : undefined;
  const input_tokens =
    typeof raw.input_tokens === "number"
      ? (raw.input_tokens as number)
      : undefined;
  const output_tokens =
    typeof raw.output_tokens === "number"
      ? (raw.output_tokens as number)
      : undefined;
  const tool_rounds =
    typeof raw.tool_rounds === "number"
      ? (raw.tool_rounds as number)
      : toolCallCount > 0
        ? toolCallCount
        : undefined;

  // input_tps (B7 round-coherence parity with live `flushTurnPerf`): the rate
  // = prompt-tokens / prefill-time is only physically meaningful when the
  // numerator (prompt size) and denominator (ttft) are the SAME round. `ttft_ms`
  // is round-0's prefill latency, so the numerator must be ROUND-0's prompt.
  // The backend tail-appends `usage.first_round_prompt_tokens` (round-0's
  // _extract_usage-corrected prompt) for exactly this — round-coherent with
  // ttft on BOTH single- AND multi-round turns, so we recompute input_tps in
  // ALL cases (matching live, which no longer omits multi-round).
  //
  // NOTE: the numerator comes from `usage.first_round_prompt_tokens`, NOT the
  // persisted `perf.input_tokens` (which `build_assistant_meta` sets to the
  // cross-round SUM `prompt_tokens` — wrong for a rate). Fallback chain for
  // legacy sessions lacking the field: first_round → prompt_tokens →
  // (total − completion). The `[I] N tokens` TOTAL display is unaffected
  // (it uses `usage.last_round_prompt_tokens` elsewhere).
  const rateInTok: number | undefined =
    usage != null
      ? (usage.first_round_prompt_tokens ??
        usage.prompt_tokens ??
        (typeof usage.total_tokens === "number"
          ? usage.total_tokens - (usage.completion_tokens ?? 0)
          : undefined))
      : input_tokens;
  let input_tps: number | undefined;
  if (
    rateInTok !== undefined &&
    ttft_ms !== undefined &&
    ttft_ms > 0
  ) {
    input_tps = Math.round((rateInTok / (ttft_ms / 1000)) * 10) / 10;
  }

  // output_tps (B6 generation-rate): the live path sums each round's actual
  // generation span (first→last text chunk) to EXCLUDE inter-round
  // tool-execution waits. Those per-round spans are transient live state and
  // are NOT persisted, so on reload we can only approximate with the legacy
  // `total_ms − ttft_ms` phase. On a multi-round turn this denominator still
  // includes the tool waits, so the reloaded output_tps is a conservative
  // LOWER bound (never an over-estimate); the precise value is shown live the
  // moment the turn completes. Single-round turns are exact either way (no
  // tool-wait to exclude). NOTE: do NOT "fix" this by inventing a fake span on
  // reload — a conservative real bound beats a fabricated precise-looking one
  // (State-Truth-First).
  let output_tps: number | undefined;
  if (output_tokens !== undefined && total_ms !== undefined) {
    const outPhaseMs =
      ttft_ms !== undefined ? Math.max(0, total_ms - ttft_ms) : total_ms;
    if (outPhaseMs > 0) {
      output_tps =
        Math.round((output_tokens / (outPhaseMs / 1000)) * 10) / 10;
    }
  }

  return {
    ...(ttft_ms !== undefined ? { ttft_ms } : {}),
    ...(total_ms !== undefined ? { total_ms } : {}),
    ...(input_tokens !== undefined ? { input_tokens } : {}),
    ...(output_tokens !== undefined ? { output_tokens } : {}),
    ...(input_tps !== undefined ? { input_tps } : {}),
    ...(output_tps !== undefined ? { output_tps } : {}),
    ...(tool_rounds !== undefined ? { tool_rounds } : {}),
  };
}

/** Map raw `MessageItem` rows into `ChatMessage[]`. */
export function mapHistoryItems(
  items: ReadonlyArray<HistoryMessageItem>,
  convId: string,
): ChatMessage[] {
  return items.map((m) => {
    const role: ChatMessageRole = _HISTORY_ALLOWED_ROLES.has(m.role)
      ? (m.role as ChatMessageRole)
      : "assistant";
    const created = Date.parse(m.created_at);
    // V1-parity meta envelope (P1). `request_id` is read by ChatMessageList
    // off `msg.meta.request_id`; `perf` / `subAgentBlocks` are top-level
    // ChatMessage fields the live stream commits, so lift them out of the
    // persisted meta to match the live-stream message shape exactly.
    const meta = (m.meta && typeof m.meta === "object" ? m.meta : null) as
      | Record<string, unknown>
      | null;
    const rawPerf = meta?.["perf"];
    const subAgentBlocks = meta?.["subAgentBlocks"];
    // Keep only `request_id` (and any non-lifted keys) in the rendered
    // `meta`; perf / subAgentBlocks are surfaced as their own fields.
    let renderMeta: Record<string, unknown> | undefined;
    if (meta) {
      // `round_index` is a backend grouping aid stamped on per-round assistant
      // messages (_streaming_helpers.py `build_tool_call_message`, used by
      // `_reinsert_injected_messages` to position a reloaded mid-turn injection
      // at its inter-round seam). It is NOT a render field — strip it so the
      // reloaded `meta` shape matches the live-committed shape (which strips the
      // camelCase `roundIndex` transient in messageCommit.ts). Order is already
      // correct: rows render in backend array order (this mapper is a 1:1
      // `items.map`, no sort/regroup), and the backend now persists injections
      // in their correct inter-round array position.
      const { perf: _p, subAgentBlocks: _s, round_index: _ri, ...rest } = meta;
      void _p;
      void _s;
      void _ri;
      if (Object.keys(rest).length > 0) renderMeta = rest;
    }
    // Rehydrate perf with derived tok/sec fields (V1 parity — V1 persists
    // the fully-computed perf; V2 backend persists raw values and we
    // recompute the derived rates here on reload).
    const toolCallCount = m.tool_calls?.length ?? 0;
    const perf =
      rawPerf && typeof rawPerf === "object"
        ? _rehydratePerf(rawPerf as Record<string, unknown>, toolCallCount, m.usage)
        : undefined;
    // Normalise the persisted content sentinels back to "" so the rehydrated
    // message shape matches the live-stream shape exactly:
    // * ``"[tool_calls]"`` — legacy tool-call-only assistant message
    //   (_streaming_helpers.py:214). The live stream commits such a message
    //   with an EMPTY content (messageCommit.ts) and only renders the
    //   ToolExecPanel — never a text bubble.
    // * ``"[subagent_summary]"`` — SUBAGENT-RELOAD-PERSIST-INDEPENDENT-MSG
    //   (2026-07-02) sentinel for the DEDICATED sub-agent-blocks message
    //   emitted by :meth:`_build_subagent_summary_message`. The live stream
    //   opens this message with empty content (only ``subAgentBlocks``
    //   accumulated in place); the sentinel exists only because
    //   ``MessageContent.text`` cannot be empty (domain constraint).
    // V1 parity: V1 has no such sentinels at all — tool-call / sub-agent-
    // blocks messages render only their cards (index.html:452).
    const normalisedText =
      m.text === "[tool_calls]" || m.text === "[subagent_summary]"
        ? ""
        : m.text;
    return {
      id: m.id,
      role,
      content: normalisedText,
      createdAt: Number.isFinite(created) ? created : Date.now(),
      conversationId: convId,
      ...(m.tool_calls && m.tool_calls.length > 0
        ? { toolCalls: m.tool_calls.map((c) => ({ ...c })) }
        : {}),
      ...(m.usage && typeof m.usage === "object"
        ? { usage: { ...m.usage } }
        : {}),
      ...(m.model_id ? { modelId: m.model_id } : {}),
      ...(m.model_provider ? { modelProvider: m.model_provider } : {}),
      // Discussion speaker id (V2 multi-agent). The role name + avatar colour
      // are resolved later in `loadHistoryMessages`, after the participant
      // roster is re-hydrated (this pure mapper has no roster context).
      ...(m.sender_id ? { senderId: m.sender_id } : {}),
      ...(renderMeta ? { meta: renderMeta } : {}),
      ...(perf !== undefined ? { perf } : {}),
      ...(Array.isArray(subAgentBlocks) && subAgentBlocks.length > 0
        ? { subAgentBlocks: subAgentBlocks as SubAgentBlock[] }
        : {}),
    };
  });
}
