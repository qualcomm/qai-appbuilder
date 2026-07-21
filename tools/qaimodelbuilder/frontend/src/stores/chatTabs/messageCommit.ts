// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Turn-commit builders for the chat store (single-track model, V1 parity).
 *
 * Single-track refactor: live streaming no longer accumulates tool cards /
 * sub-agent blocks into separate bottom-of-stream buffers. Instead each
 * agentic round pushes its own `assistant{content:<lead-in>, toolCalls}`
 * message straight into `tab.messages` while streaming (marked
 * `meta.streaming:true`), and sub-agent frames build a streaming
 * `subAgentBlocks` message the same way — V1 useChat.js:2455-2520 (one ordered
 * `messages` list). So by the time the turn settles, those per-round messages
 * are ALREADY in place and in the right order; they need only be *finalized*:
 *
 *   - flip any residual `running` tool card to a terminal state (V1
 *     finally-block parity, useChat.js:2538-2554) — `done` on a clean finish,
 *     `error` on a user abort,
 *   - drop the transient `meta.streaming` marker (and, on abort, flip any
 *     still-running sub-agent block running→error),
 *   - commit the trailing summary text (`streamingContent`, the text the model
 *     produced AFTER the last tool round) as the LAST assistant message,
 *   - stamp usage / perf / request_id / model onto the turn's last message.
 *
 * The result matches the reloaded-from-DB shape byte-for-byte: the backend
 * persists one assistant message per round (`_streaming_helpers.py`
 * `build_tool_call_message`) + one final summary message, exactly what we hold
 * after finalization. These builders are pure (a tab snapshot + a fresh id in,
 * the next `messages` array out); the store actions stay thin.
 */
import type {
  ChatMessage,
  ChatTab,
  ChatToolCall,
  ChatMessageUsage,
  ChatMessagePerf,
  SubAgentToolCall,
} from "../_chatTabsTypes";

/** Was this message pushed by the live single-track stream (and so needs
 *  finalizing)? Marked `meta.streaming:true` by the frame handlers. */
function isStreamingMessage(m: ChatMessage): boolean {
  return (
    m.meta !== undefined &&
    (m.meta as { streaming?: unknown }).streaming === true
  );
}

/** Flip any tool calls left in `running` to a terminal state.
 *
 * V1 parity (useChat.js:2538-2554): V1 flipped `toolRunning=false` in the
 * `finally` of each tool's `await`, so committed history NEVER showed a
 * spinning tool. A `tool_result` frame can be lost when the turn leaves
 * `streaming` early (error / abort before the matching result, or a pairing
 * miss) — leaving a card stuck `running` with a forever-counting timer
 * (State-Truth-First violation). On turn end we force every residual `running`
 * card to a terminal state: `done` on a clean finish, `error` on a user abort
 * (mirrors the running→error sub-agent flip on aborts). */
function finalizeRunningToolCalls(
  calls: readonly ChatToolCall[],
  terminal: "done" | "error",
): ChatToolCall[] {
  return calls.map((c) => {
    if (c.status !== "running") return { ...c };
    const next: ChatToolCall = { ...c, status: terminal };
    // P2: a `timedFromGeneration` card aborted/forced-terminal here never
    // received a final tool_result, so its `totalMs` was never computed. Derive
    // it now from the front-end generation start so the card shows "spent N"
    // instead of falling back to a wall-clock timestamp. Prefer an existing
    // `totalMs` (already authoritative); else generationMs(+0 exec) ; else the
    // `generationStartedAt → now` span (front-end approximation).
    if (c.timedFromGeneration === true && c.totalMs === undefined) {
      if (typeof c.generationMs === "number") {
        next.totalMs = c.generationMs;
      } else if (typeof c.generationStartedAt === "number") {
        next.totalMs = Math.max(0, Date.now() - c.generationStartedAt);
      }
    }
    // No longer generating args once terminal.
    if (c.argsStreaming === true) {
      next.argsStreaming = undefined;
      next.argsCharCount = undefined;
    }
    return next;
  });
}

/** Finalize ONE sub-agent inner tool card on turn settlement.
 *
 * A sub-agent block hosts its own ToolExecPanel cards inside
 * `turns[].tools[]` (`SubAgentToolCall`). These are settled live by
 * `handleSubagentToolResult` (a `subagent_tool_result` frame sets
 * `result` + `status:done/error`). But when the PARENT turn aborts (user
 * Stop → cascade-interrupt), the parent merge loop stops forwarding
 * sub-agent frames (`streaming.py` breaks out), so an in-flight tool (e.g.
 * a long `exec` running a PowerShell child) NEVER receives its terminal
 * `subagent_tool_result` frame — leaving the inner card stuck `running`
 * (spinner + red stop button + forever-counting timer), even after the
 * backend fix kills the subprocess and the block itself flips to
 * `error`. So on a user abort (`terminal === "error"`) we flip any inner
 * tool still `running` (no `result` yet — the type contract at
 * `_chatTabsTypes.ts` SubAgentToolCall.status) to a terminal `error` so the
 * card renders settled ("已中断") instead of spinning. On a clean finish
 * (`terminal === "done"`) inner tools are left as-is — a genuinely-running
 * tool on a clean parent finish is a contradiction that resolves via its
 * own frame, and we must never rewrite a legitimately settled card. A tool
 * that already carries a `result` (settled) is never touched. */
function finalizeRunningSubAgentTool(
  t: SubAgentToolCall,
  terminal: "done" | "error",
): SubAgentToolCall {
  // Only a user abort forces residual running cards terminal; a clean finish
  // leaves them untouched (their own frame settles them — no-regression).
  if (terminal !== "error") return { ...t };
  // Already settled (has a result, or an explicit terminal status) → leave it.
  if (t.result !== undefined) return { ...t };
  if (t.status === "done" || t.status === "error") return { ...t };
  // Residual running (no result; status running or undefined→derived running):
  // flip to error/interrupted so the card stops spinning.
  return { ...t, status: "error" as const, ok: false };
}

/** Strip the transient `meta.streaming` marker (and the transient
 *  `meta.roundIndex` grouping tag stamped on live per-round messages) from a
 *  message's meta, keeping any other meta keys (e.g. `request_id`). Returns
 *  `undefined` when nothing meaningful remains so the message shape matches a
 *  reloaded one. The backend does not persist `streaming`; it DOES persist a
 *  snake_case `round_index` on per-round assistant messages, but the history
 *  mapper (historyMapper.ts) strips that on reload, so both the live-committed
 *  and the reloaded message carry no round-index meta — shapes still match. */
function stripStreamingMeta(
  meta: Record<string, unknown> | undefined,
): Record<string, unknown> | undefined {
  if (meta === undefined) return undefined;
  const { streaming: _s, roundIndex: _ri, ...rest } = meta;
  void _s;
  void _ri;
  return Object.keys(rest).length > 0 ? rest : undefined;
}

/** Finalize one streaming round/sub-agent message into its settled shape:
 *  flip residual running cards, flip running sub-agent blocks (on abort), and
 *  drop the `streaming` marker. Pure — returns a new message. */
function finalizeStreamingMessage(
  m: ChatMessage,
  terminal: "done" | "error",
): ChatMessage {
  const nextMeta = stripStreamingMeta(m.meta as Record<string, unknown> | undefined);
  const next: ChatMessage = {
    ...m,
    ...(m.toolCalls !== undefined
      ? { toolCalls: finalizeRunningToolCalls(m.toolCalls, terminal) }
      : {}),
    ...(m.subAgentBlocks !== undefined
      ? {
          subAgentBlocks: m.subAgentBlocks.map((b) => ({
            ...b,
            // Deep-copy the ordered per-round turns (and each turn's tool
            // list) so the committed message owns an independent snapshot.
            turns: b.turns.map((tu) => ({
              ...tu,
              tools: tu.tools.map((t) => finalizeRunningSubAgentTool(t, terminal)),
            })),
            // Flip any still-streaming sub-agent block to a TERMINAL state on
            // turn settlement so the optimistic `aborting` intermediate state
            // (set by `interruptSubAgent` when the user pressed ⏹) never leaks
            // into a committed message:
            //   * On a user abort (`terminal === "error"`): `running`/`aborting`
            //     → `error` (V1 parity — the interrupt icon, `error:
            //     "interrupted"` when absent).
            //   * On a clean finish (`terminal === "done"`): `aborting` → `done`
            //     (the rare race where the user pressed ⏹ but the backend's
            //     last frame still landed as a normal completion before the
            //     abort signal took effect — the block is in fact done, not
            //     stuck "stopping…"). `running` blocks on a clean finish are
            //     left as-is (their own terminal frame settles them; legacy
            //     V1 parity preserved).
            ...(terminal === "error" &&
            (b.status === "running" || b.status === "aborting")
              ? {
                  status: "error" as const,
                  ...(b.error === undefined ? { error: "interrupted" } : {}),
                }
              : terminal === "done" && b.status === "aborting"
                ? { status: "done" as const }
                : {}),
          })),
        }
      : {}),
  };
  // Re-assign meta explicitly so a stripped-to-empty meta becomes absent.
  if (nextMeta === undefined) {
    delete (next as { meta?: unknown }).meta;
  } else {
    (next as { meta?: Record<string, unknown> }).meta = nextMeta;
  }
  return next;
}

/** Fields stamped onto the turn's LAST message (V1 useModels.js:53-70 +
 *  useChat.js:2402 — model + prompt-snapshot request_id + usage/perf). */
function turnStampFields(
  tab: ChatTab,
  usage: ChatMessageUsage | null,
  perf: ChatMessagePerf | null,
  requestId: string | null,
  baseMeta?: Record<string, unknown>,
): Partial<ChatMessage> {
  const meta: Record<string, unknown> = { ...(baseMeta ?? {}) };
  if (requestId !== null && requestId !== undefined) {
    meta["request_id"] = requestId;
  }
  // Model id for the turn's final/trailing message. In a multi-Agent
  // discussion the active SPEAKER's own model takes precedence over the
  // tab's selected model — otherwise this stamp (spread AFTER the sender
  // block in assembleSettled) would clobber the speaker's model with the
  // tab default (the bug where every discussion bubble showed the bottom-
  // left selected model instead of each role's own model). For single-Agent
  // chat `streamingSenderModelId` is null → falls back to `tab.modelId`
  // (unchanged behaviour).
  // Pro (MB Pro) mode: the outgoing transport overrides model_id to
  // `query::mb_pro` (useChatTransport), and the BACKEND persists the assistant
  // message with `model_id="query::mb_pro"` (streaming.py _finalize). But
  // `tab.modelId` is the user's separately-selected real model — so without
  // this branch the LIVE-committed message would be stamped with the real
  // model while the SAME message reloaded from DB carries `query::mb_pro`,
  // an inconsistency that breaks per-message source detection (sender label /
  // avatar). Stamp `query::mb_pro` so realtime and reload agree, making
  // `msg.modelId === "query::mb_pro"` a reliable per-message key (symmetric
  // with how CEBot is `query::cebot` in both paths).
  //
  // ORDER MATTERS: a multi-Agent discussion speaker's own model
  // (`streamingSenderModelId`) ALWAYS wins — discussion and Pro mode are
  // independent and CAN coexist (activeMode==='pro' + discussion.isDiscussion),
  // so checking Pro first would clobber each speaker's model with
  // `query::mb_pro` and reintroduce the "every discussion bubble shows the
  // same model" regression this stamp was designed to avoid. So: speaker model
  // first, then Pro, then the tab default. (`activeMode` is still 'pro' at
  // commit time; we only use it to derive the stamp — the durable per-message
  // judgment key is the stamped `msg.modelId`, not the tab-level activeMode.)
  const stampModelId = tab.streamingSenderModelId
    ? tab.streamingSenderModelId
    : tab.activeMode === "pro"
      ? "query::mb_pro"
      : tab.modelId;
  return {
    ...(usage !== null ? { usage } : {}),
    ...(perf !== null ? { perf } : {}),
    ...(stampModelId ? { modelId: stampModelId } : {}),
    ...(tab.modelProvider ? { modelProvider: tab.modelProvider } : {}),
    ...(Object.keys(meta).length > 0 ? { meta } : {}),
  };
}

/**
 * Build the post-`done` messages array for a finished turn.
 *
 * Single-track: per-round tool / sub-agent messages are already in
 * `tab.messages` (pushed live by the frame handlers). We finalize those, then
 * append the trailing summary text (`streamingContent`) as the last assistant
 * message and stamp usage/perf/request_id/model onto the turn's last message.
 * Matches the reloaded-from-DB shape (one message per round + a final summary).
 */
export function buildConfirmDoneMessages(
  tab: ChatTab,
  newMessageId: string,
): ChatMessage[] {
  return assembleSettled(tab, newMessageId, "done", false);
}

/**
 * Build the post-abort / post-error messages array.
 *
 * Same single-track finalization as `done`, but residual running tool cards
 * flip to `error`, still-running sub-agent blocks flip running→error, and the
 * trailing summary message (if any) is tagged `meta.interrupted=true` (the
 * render layer appends the localized interruptedMark — V1 useChat.js:2696).
 *
 * `alwaysCommit=true` (user abort) commits a trailing interrupted message even
 * with no trailing text, so the user always sees an abort indicator (V1
 * chat_handler.py:704 always yields "\n\n[操作已被用户中断]"). When false
 * (server error) a trailing message is added ONLY if there was trailing text;
 * a zero-content server error commits NO empty bubble (it surfaces via the
 * per-message + tab error banners instead). The per-round messages are still
 * finalized either way.
 */
export function buildConfirmAbortMessages(
  tab: ChatTab,
  newMessageId: string,
  alwaysCommit = false,
): ChatMessage[] {
  return assembleSettled(tab, newMessageId, "error", true, alwaysCommit);
}

/** Shared assembly for done / abort / error settlement. */
function assembleSettled(
  tab: ChatTab,
  newMessageId: string,
  terminal: "done" | "error",
  interrupted: boolean,
  alwaysCommit = false,
): ChatMessage[] {
  // 1) Finalize every live streaming round/sub-agent message in place.
  const finalized = tab.messages.map((m) =>
    isStreamingMessage(m) ? finalizeStreamingMessage(m, terminal) : m,
  );
  const hadStreamingMessages = tab.messages.some(isStreamingMessage);

  // 2) The trailing text the model produced AFTER the last tool round.
  const finalText = tab.streamingContent;
  const usage = tab.streamingUsage;
  const perf = tab.streamingPerf;
  const requestId = tab.streamingRequestId;

  // 3) Decide whether to add a trailing summary message.
  //    - done: add when there is trailing text OR (no streaming messages at
  //      all AND there is usage/perf — a plain text turn with metadata).
  //    - abort/error: add when trailing text exists, or `alwaysCommit` (a USER
  //      abort always commits a "[操作已被用户中断]" indicator, V1
  //      chat_handler.py:704). A zero-content SERVER error (`alwaysCommit`
  //      false, no trailing text) must NOT fabricate an empty assistant bubble:
  //      it already surfaces via the per-message `sendError` banner + the
  //      tab-level error banner, and the empty row is never persisted, so it
  //      would only pollute the in-memory view (and accumulate one orphan per
  //      retry) — a State-Truth-First violation (UI shows a row the DB never
  //      has). Round/sub-agent messages are still finalized above either way.
  const hasTrailingText = finalText.length > 0;
  let addTrailing: boolean;
  if (interrupted) {
    addTrailing = hasTrailingText || alwaysCommit;
  } else {
    addTrailing =
      hasTrailingText ||
      (!hadStreamingMessages && (usage !== null || perf !== null));
  }

  if (!addTrailing) {
    // No trailing message. Stamp usage/perf/request_id/model onto the LAST
    // finalized streaming message so the metrics line / 📄 button still show.
    if (hadStreamingMessages && (usage !== null || perf !== null || requestId !== null)) {
      const lastIdx = lastStreamingIndex(tab.messages);
      if (lastIdx >= 0) {
        const target = finalized[lastIdx]!;
        finalized[lastIdx] = {
          ...target,
          ...turnStampFields(
            tab,
            usage,
            perf,
            requestId,
            target.meta as Record<string, unknown> | undefined,
          ),
        };
      }
    }
    return finalized;
  }

  // 4) Append the trailing summary as the turn's last assistant message,
  //    carrying the turn-level stamps. On abort/error tag it interrupted.
  const trailing: ChatMessage = {
    id: newMessageId,
    role: "assistant",
    content: finalText,
    createdAt: Date.now(),
    ...(tab.conversationId !== null
      ? { conversationId: tab.conversationId }
      : {}),
    // Multi-Agent discussion (block-5): attribute the trailing summary bubble to
    // the last live speaker so it renders with that participant's avatar/name/
    // color. Undefined for ordinary single-agent turns (zero behaviour change).
    ...(tab.streamingSenderId !== null
      ? {
          senderId: tab.streamingSenderId,
          ...(tab.streamingSenderName !== null
            ? { senderName: tab.streamingSenderName }
            : {}),
          ...(tab.streamingSenderColor !== null
            ? { senderColor: tab.streamingSenderColor }
            : {}),
          // Stamp the discussion speaker's effective model id so the bubble
          // shows "name · model" (V2 enhancement 2026-06-21). Single-agent
          // chat continues to read ``modelId`` from ``turnStampFields`` →
          // unchanged for non-discussion turns.
          ...(tab.streamingSenderModelId !== null
            ? { modelId: tab.streamingSenderModelId }
            : {}),
        }
      : {}),
    ...turnStampFields(
      tab,
      usage,
      perf,
      requestId,
      interrupted ? { interrupted: true } : undefined,
    ),
  };
  return [...finalized, trailing];
}

/** Index of the last live streaming message in the ORIGINAL messages array
 *  (so we stamp the right finalized entry). -1 when none. */
function lastStreamingIndex(messages: readonly ChatMessage[]): number {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m !== undefined && isStreamingMessage(m)) return i;
  }
  return -1;
}
