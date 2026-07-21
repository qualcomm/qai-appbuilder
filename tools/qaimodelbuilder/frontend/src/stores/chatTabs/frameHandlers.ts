// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * SSE stream-frame handlers for the chat store (F3 cohesion split).
 *
 * `chatTabs.applyFrame` was a ~330-line `switch` state machine that mixed
 * the per-`frame_type` reducer logic with the action's dispatch shell.
 * This module extracts each branch into a small, pure(-ish) handler keyed
 * by `frame_type` so:
 *   - `applyFrame` shrinks to a guard + a table lookup + dispatch, and
 *   - each frame kind's logic is independently readable / testable.
 *
 * Handlers are written in a reducer style: they receive the current `tab`
 * snapshot, the incoming `frame`, and a small `ctx` of store-action
 * callbacks (already bound to the active tab id by `applyFrame`). They
 * never touch Pinia directly — every mutation goes through `ctx.patchTab`
 * / `ctx.appendStreamingChunk` / `ctx.setActiveMode`, so the store keeps
 * sole ownership of reactive writes and the V1-parity behaviour is
 * byte-for-byte unchanged (only relocated).
 *
 * V1 truth: `useChat.js` SSE handling — chunk (assistant text), tool_call /
 * tool_result (tool cards), end (usage + prompt-snapshot request id),
 * tool_mode_changed (1324-1332), turn_warning (1422-1432), subagent_*
 * (1345-1408), agent_summary (1402-1408).
 */
import type { ChatStreamFrame } from "@/types/streaming";
import type {
  ChatMessage,
  ChatTab,
  ChatToolCall,
  ChatMessageUsage,
  ImplementationItemVM,
  SubAgentBlock,
  SubAgentIndexEntry,
  SubAgentToolCall,
  SubAgentTurn,
  ToolModeKey,
} from "../_chatTabsTypes";
import { discussionColorToken } from "../_chatTabsTypes";
import { applyQuestion, applyTodoWrite } from "./harnessToolFrames";
import { renderBoundedPreview } from "./toolOutputPreview";

// ---------------------------------------------------------------------------
// Frame-local helpers (moved verbatim from chatTabs.ts — used only here).
// ---------------------------------------------------------------------------

/** Coerce a tool result (string / object / number / null) into a
 *  displayable string for the tool card output pane. */
export function stringifyToolResult(result: unknown): string {
  if (result === null || result === undefined) return "";
  if (typeof result === "string") return result;
  try {
    return JSON.stringify(result, null, 2);
  } catch {
    return String(result);
  }
}

/** Detect the backend's synthetic error sentinels so the tool card can
 *  flag itself as failed (streaming.py:1181/1200 — `[guardrail_blocked]`
 *  / `[tool_error]`). */
export function isToolErrorOutput(output: string): boolean {
  return (
    output.startsWith("[tool_error]") ||
    output.startsWith("[guardrail_blocked]")
  );
}

/** Map a backend-reported tool mode (e.g. from a `tool_mode_changed`
 *  SSE frame) onto the frontend's `ToolModeKey` set.
 *
 *  Backend auto-detect emits Python-style identifiers — notably
 *  ``"model_build"`` and ``"model_builder"`` (system_prompt_builder.py
 *  :245-248) — whereas the frontend toolbar uses the hyphenated
 *  ``"model-build"``. Normalise so the toolbar receives a key it
 *  recognises; return ``null`` for unknown values so the caller can
 *  ignore the frame instead of corrupting toolbar state.
 */
export function normaliseDetectedToolMode(raw: string): ToolModeKey | null {
  // Synonyms the backend may emit for the same logical mode.
  const aliased: Record<string, ToolModeKey> = {
    "model_build": "model-build",
    "model_builder": "model-build",
    "model-build": "model-build",
    "model_hub": "model-hub",
    "model-hub": "model-hub",
    "app-builder": "app-builder",
    "app_builder": "app-builder",
    "code": "code",
    "translate": "translate",
    "ppt": "ppt",
  };
  const v = aliased[raw];
  return v ?? null;
}

// ---------------------------------------------------------------------------
// Handler contract
// ---------------------------------------------------------------------------

/** Store-action callbacks a frame handler may use, pre-bound to the active
 *  tab id by `applyFrame`. Handlers never call Pinia directly. */
export interface FrameHandlerContext {
  /** `store._patchTab(tabId, patch)` bound to the active tab. */
  patchTab: (patch: Partial<ChatTab>) => void;
  /** `store.appendStreamingChunk(tabId, text, backfill)` bound to the active
   *  tab. `backfill=true` (broadcaster cursor=0 replay — sub-agent cold-open
   *  or active-run WS reconnect) accumulates into the coalescing buffer but
   *  SUPPRESSES the rAF/timer flush; the backfill→live boundary in
   *  `applyFrame`'s caller emits one synchronous flush so the historical
   *  burst lands as a single reactive write rather than逐段 replaying. */
  appendStreamingChunk: (text: string, backfill?: boolean) => void;
  /** `store.appendRoundChunk(tabId, messageId, text, backfill)` bound to the
   *  active tab — rAF-coalesced append into a SPECIFIC round message's content
   *  (the round_index zero-inference path). `backfill` has the same semantics
   *  as on `appendStreamingChunk`: cursor=0 replay text accumulates but is
   *  NOT scheduled, so the user does not see the trailing transcript replayed
   *  逐段 after their HTTP snapshot already rendered it. */
  appendRoundChunk: (messageId: string, text: string, backfill?: boolean) => void;
  /** `store.setActiveMode(tabId, mode)` bound to the active tab. */
  setActiveMode: (mode: ToolModeKey | null) => void;
  /** Monotonic client message-id generator (V1 parity). */
  nextMessageId: () => string;
  /** Absorb a streaming tool-card output `delta` into the card's BOUNDED
   *  preview buffer (frozen head + rolling tail; middle folded) and schedule a
   *  throttled reactive write of the rendered preview into the card's `output`
   *  (at most one paint per animation frame). Returns the current bounded
   *  preview so a freshly-seeded card can store it immediately. `cardKey` is the
   *  card's stable identity (`callId ?? originating frame id`). Bounds both
   *  memory (card never holds the whole multi-MB stream) and render cost
   *  (no per-frame full-`messages`-array replacement). */
  bufferToolOutput: (cardKey: string, delta: string) => string;
  /** Synchronously flush + drop a card's bounded-preview buffer when its stream
   *  settles (terminal `tool_result`), so the final paint is not swallowed by
   *  the throttle and a reused card key starts fresh next turn. */
  flushToolOutput: (cardKey: string) => void;
  /** `store._upsertSubAgentIndexEntry(entry)` — SubAgentRail index cache
   *  incremental update. Called by `subagent_start` so a newly-spawned
   *  sub-agent chip appears on the rail IMMEDIATELY (grey/running), without
   *  waiting for the user to switch main tabs or open the sub-agent's tab.
   *  See `SubAgentIndexEntry` docstring for the full lifecycle. */
  upsertSubAgentIndexEntry: (entry: SubAgentIndexEntry) => void;
  /** `store._updateSubAgentIndexStatus(subagentId, status)` — cheap fast-path
   *  for `subagent_done` / other terminal transitions so a closed sub-agent's
   *  chip on the rail reflects the freshest terminal state. No-op when the
   *  index entry doesn't exist yet (a later upsert / fetch will create it). */
  updateSubAgentIndexStatus: (subagentId: string, status: string) => void;
  /** True when the frame being dispatched is part of a broadcaster cursor=0
   *  replay (the transcript-so-far the user already saw via the HTTP
   *  snapshot). Handlers that mutate visible text MUST pass this through
   *  to the append helpers so the coalescing layer suppresses逐段 flushing.
   *  Defaults to false (LIVE frame) when the caller does not set it, so
   *  pre-backfill-aware tests / call sites keep the old behaviour. */
  isBackfill?: boolean;
}

/** A single per-`frame_type` reducer. Receives the current tab snapshot,
 *  the frame, and the bound action callbacks. */
export type FrameHandler = (
  tab: ChatTab,
  frame: ChatStreamFrame,
  ctx: FrameHandlerContext,
) => void;

/** Narrow a frame payload to a plain object, or null. */
function asPayload(frame: ChatStreamFrame): Record<string, unknown> | null {
  const p = frame.payload as Record<string, unknown> | null;
  return p !== null && typeof p === "object" ? p : null;
}

// ---------------------------------------------------------------------------
// Per-kind handlers
// ---------------------------------------------------------------------------

/** Read the backend-stamped agentic-loop `round_index` off a frame payload,
 *  or `null` when absent (old frames / non-agentic turns → callers fall back
 *  to the legacy single-buffer behaviour). 0-based; the LLM call that produced
 *  the text / issued the tool call (a tool_result shares its call's round). */
function readRoundIndex(payload: Record<string, unknown>): number | null {
  const ri = payload.round_index;
  return typeof ri === "number" && Number.isFinite(ri) && ri >= 0 ? ri : null;
}

/** Read the backend-stamped per-round prompt-snapshot `request_id` off a frame
 *  payload, or `""` when absent. Per-round snapshot (V1 parity): every agentic
 *  round saves its OWN snapshot, so each round's CHUNK / TOOL_CALL / TOOL_RESULT
 *  frame carries that round's `request_id` (backend `_stamp_request_id`). The
 *  frontend stamps it onto the round's assistant message `meta.request_id` so
 *  each tool card's 📄 button opens ITS round's prompt — different rounds show
 *  different prompts. Empty when the snapshot store is not wired / legacy
 *  frames (no 📄 button for that round, as before). */
function readRequestId(payload: Record<string, unknown>): string {
  const rid = payload.request_id;
  return typeof rid === "string" && rid !== "" ? rid : "";
}

/** Read the Multi-Agent discussion (block-5) `sender_id` off a frame payload,
 *  or `undefined` when absent (ordinary single-agent turns — every chunk /
 *  tool_call / tool_result frame may carry one in a discussion). When present
 *  it identifies the named participant that produced the frame. */
function readSenderId(payload: Record<string, unknown>): string | undefined {
  const sid = payload.sender_id;
  return typeof sid === "string" && sid !== "" ? sid : undefined;
}

/** Resolve the participant authorship fields to stamp onto a freshly-opened
 *  per-round / per-speaker assistant message (block-5). Prefers the frame's own
 *  `sender_id`; falls back to the tab's live speaker anchor (set by the most
 *  recent `speaker_changed` frame) so a frame that omitted `sender_id` still
 *  attributes to the current speaker. Returns an empty object for ordinary
 *  single-agent turns (no sender anywhere) so the message shape is unchanged. */
function senderFields(
  tab: ChatTab,
  payload: Record<string, unknown>,
): Partial<Pick<ChatMessage, "senderId" | "senderName" | "senderColor">> {
  const sid = readSenderId(payload) ?? tab.streamingSenderId ?? undefined;
  if (sid === undefined) return {};
  return {
    senderId: sid,
    ...(tab.streamingSenderName !== null
      ? { senderName: tab.streamingSenderName }
      : {}),
    ...(tab.streamingSenderColor !== null
      ? { senderColor: tab.streamingSenderColor }
      : {}),
  };
}

/** Resolve the modelId to stamp on a round message AT CREATION TIME.
 *
 *  Without this, round messages created by the streaming frame handlers
 *  (handleReasoning / handleChunk / handleToolCall) carry no `modelId`, so
 *  during streaming the per-message sender label + avatar fall back to
 *  `stripPrefix(tab.modelId)` + the default star icon (e.g. a CEBot turn shows
 *  "cebot" + star mid-stream, only flipping to "CEBot" + branded icon after
 *  commit — and for messages that aren't the last/trailing one, never flipping
 *  at all). Stamping the modelId when the round message is BORN makes the
 *  query-service brand (query::cebot / query::mb_pro) apply consistently in
 *  both the live stream and the committed message.
 *
 *  Priority MUST match the commit-time stamp (`messageCommit.ts` turnStampFields):
 *  discussion speaker's own model > Pro (query::mb_pro) > tab.modelId. Speaker
 *  first so a multi-Agent discussion bubble keeps each speaker's real model
 *  (never clobbered by query::mb_pro). Returns `{}` when nothing resolves so
 *  the message shape is unchanged for legacy/unknown turns. */
function roundModelFields(
  tab: ChatTab,
): Partial<Pick<ChatMessage, "modelId" | "modelProvider">> {
  const mid = tab.streamingSenderModelId
    ? tab.streamingSenderModelId
    : tab.activeMode === "pro"
      ? "query::mb_pro"
      : tab.modelId;
  return {
    ...(mid ? { modelId: mid } : {}),
    ...(tab.modelProvider ? { modelProvider: tab.modelProvider } : {}),
  };
}

const handleChunk: FrameHandler = (tab, frame, ctx) => {
  const payload = asPayload(frame);
  if (payload === null || typeof payload.text !== "string") return;
  const text = payload.text;
  if (text === "") return;
  // ── Zero-inference round routing (backend round_index) ────────────────────
  // The chunk's `round_index` (when stamped) is the LLM call that produced this
  // text. The text is bound to ITS round's assistant message IMMEDIATELY — it
  // NEVER goes to the shared bottom `streamingContent` buffer. This is the fix
  // for the "云端多轮文本累积成一块、沉到所有工具卡下方" bug: the old code parked
  // every round's pre-tool text in the single bottom buffer and only folded it
  // when that round's first tool_call arrived. When a round produced text but
  // no tool_call (or text after the tool_call, or the tool_call's fold was
  // mistimed), the text leaked across rounds and accumulated at the bottom.
  //
  // Two cases, decided ONLY by whether that round already has an open message:
  //   1. Round N's message EXISTS ⇒ append this text onto it (rAF-coalesced via
  //      `appendRoundChunk`), whether it is the round's lead-in continuing, or
  //      narration that surfaced AFTER one of the round's tool cards.
  //   2. Round N has NO message yet ⇒ OPEN one now (empty content + the round's
  //      request_id), record it in `roundMessageIds`, then append the text onto
  //      it. The round's later tool_call(s) append cards onto this same message
  //      (handleToolCall's round-open branch finds it via `roundMessageIds`).
  //      A final-summary round (no tool_call ever comes) is then ALREADY a
  //      standalone assistant message carrying just the answer text — matching
  //      the reload-from-DB shape (backend `_finalize_assistant_message`
  //      persists the trailing answer as its own assistant message).
  const ri = readRoundIndex(payload);
  if (ri !== null) {
    let openId = tab.roundMessageIds[ri];
    if (openId === undefined) {
      // A brand-new round `ri` is starting (its lead-in text arrived before any
      // tool_call). That proves rounds < ri finished on the backend; settle any
      // card still stuck "生成参数中" in those earlier rounds so it stops
      // spinning. Fold the settled messages into the same patch below.
      const settled = settleStalePriorRoundCards(tab.messages, ri);
      const baseMessages = settled ?? tab.messages;
      // Open a fresh per-round assistant message for this chunk (chunk-first:
      // the round's text arrived before any tool_call). Stamp the round's
      // prompt-snapshot request_id so a plain-text / final-summary round still
      // surfaces the 📄 button (and stream↔reload agree on the prompt).
      const id = ctx.nextMessageId();
      const reqId = readRequestId(payload);
      const roundMsg: ChatMessage = {
        id,
        role: "assistant",
        content: "",
        createdAt: Date.now(),
        ...(tab.conversationId !== null
          ? { conversationId: tab.conversationId }
          : {}),
        ...senderFields(tab, payload),
        ...roundModelFields(tab),
        meta: {
          streaming: true,
          roundIndex: ri,
          ...(reqId !== "" ? { request_id: reqId } : {}),
        },
      };
      ctx.patchTab({
        messages: [...baseMessages, roundMsg],
        roundMessageIds: { ...tab.roundMessageIds, [ri]: id },
      });
      openId = id;
    }
    // rAF-coalesced append into THIS round's message content (perf parity with
    // the bottom buffer; round switches flush the prior round first).
    // `ctx.isBackfill` flows from the broadcaster's `backfill` envelope flag
    // → the coalescing layer accumulates into the buffer but SUPPRESSES the
    // scheduled flush for backfill (cursor=0 replay) frames, so the trailing
    // transcript the user has already seen via the HTTP snapshot does not get
    // replayed逐段. The backfill→live boundary in `chatTabs.handleFrame`
    // commits the whole accumulated batch in ONE flush.
    ctx.appendRoundChunk(openId, text, ctx.isBackfill === true);
    return;
  }
  // ── Legacy / no-round_index path (old backend, non-agentic turns) ─────────
  // Capture this round's prompt-snapshot `request_id` off the CHUNK frame so a
  // PLAIN-TEXT turn (no tool_call frame ever opens a round message) can still
  // surface the 📄 button. Previously `streamingRequestId` was set ONLY by the
  // terminal END frame (handleEnd); when a text turn was INTERRUPTED (user Stop
  // / disconnect / error) the END frame never arrived, so the id was lost and
  // the interrupted message showed no 📄 — even though the backend had already
  // saved the snapshot on the first frame. CHUNK frames carry the same
  // `request_id` (backend `_stamp_request_id`), so reading it here (only when
  // not already set, so a later END/round id never gets clobbered) makes the id
  // available to `confirmDone`/interrupt commit regardless of how the turn ends
  // (§State-Truth-First: take the id from the frames actually received, not
  // from a terminal frame that may never come).
  if (tab.streamingRequestId === null || tab.streamingRequestId === "") {
    const rid = readRequestId(payload);
    if (rid !== "") ctx.patchTab({ streamingRequestId: rid });
  }
  ctx.appendStreamingChunk(text, ctx.isBackfill === true);
};

/** Force-settle tool cards stuck in the `argsStreaming` ("正在生成参数…")
 *  sub-state in round messages BELONGING TO AN EARLIER ROUND than `currentRi`.
 *
 *  State-Truth-First (rule 5 — exit paths must settle live state): a card seeded
 *  by a `generating_args` progress frame only leaves the "正在生成参数…" sub-state
 *  when ITS OWN final `tool_call` frame (flip → executing) or terminal
 *  `tool_result` frame arrives. If BOTH of those frames are lost (dropped mid-turn
 *  reconnect, a provider that never re-emits the consolidated `tool_call`, or a
 *  terminal frame that raced the tab out of `streaming`), the card spins forever
 *  ("生成参数中" + red stop button + a timer counting up) even though the turn
 *  demonstrably moved on — the exact "为什么 write 还在生成参数中" bug.
 *
 *  The proof that an earlier round completed on the backend is that a LATER round
 *  is now producing activity (a fresh `tool_call`, or a new round's lead-in
 *  `chunk`). So whenever we open / target round `currentRi`, we settle any card
 *  still in the `argsStreaming` sub-state in a round message whose `round_index`
 *  is strictly LESS than `currentRi`, flipping it to `done` (the backend
 *  progressing to more rounds means arg generation for the earlier round
 *  finished; a red error would be a misleading false-negative).
 *
 *  SCOPE (deliberately narrow): ONLY `argsStreaming` cards are touched. A plain
 *  `running` card (a tool that finished generating args and is now EXECUTING —
 *  e.g. a slow `exec` whose `tool_result` simply hasn't arrived yet) is LEFT
 *  ALONE: its args generation is done, and force-completing a genuinely-running
 *  tool would be a false state. Same-round cards (`=== currentRi`) and rounds
 *  with no `round_index` meta (legacy path) are never touched.
 *
 *  PURE: returns a NEW messages array when something was settled, or `null` when
 *  nothing changed. The caller decides how to merge it (it must use the returned
 *  array as its working base so a subsequent patch does not clobber the fix). */
function settleStalePriorRoundCards(
  messages: readonly ChatMessage[],
  currentRi: number,
  excludeMsgId?: string,
): ChatMessage[] | null {
  let changed = false;
  const next = messages.map((m) => {
    // Never touch the current round's own message (it may legitimately hold a
    // freshly-seeded, still-live argsStreaming card — e.g. an orphan seed that
    // has no roundIndex yet). The caller passes the id of the message it is
    // opening / appending to for `currentRi`.
    if (excludeMsgId !== undefined && m.id === excludeMsgId) return m;
    const mri = (m.meta as { roundIndex?: unknown } | undefined)?.roundIndex;
    // Settle a message's stuck arg-streaming cards when it provably belongs to
    // an EARLIER round than the one now starting. Two cases:
    //   (a) it carries a numeric roundIndex strictly < currentRi, OR
    //   (b) it carries NO numeric roundIndex at all — an "orphan" seed. This
    //       happens intermittently when a `generating_args` progress frame
    //       arrived without a `round_index` (or before its round was
    //       established), so `openRoundMessage(tab, null, …)` created the host
    //       message WITHOUT stamping `roundIndex`. Because this function only
    //       runs at the instant a NEW round `currentRi` is opening, any
    //       pre-existing un-tagged message is necessarily from an earlier phase
    //       of the turn, so its still-"生成参数中" card is stale and must settle.
    //       (This was the gap behind the intermittent "上面 write 还在生成参数中
    //       就跑下面了" window — the card DID settle at turn end, but not mid-turn
    //       because case (b) was previously skipped.)
    // A message whose roundIndex is >= currentRi (the current/later round) is
    // never touched.
    const isEarlierNumbered = typeof mri === "number" && mri < currentRi;
    const isOrphanUntagged = mri === undefined || mri === null;
    if (!isEarlierNumbered && !isOrphanUntagged) return m;
    const cards = m.toolCalls;
    if (cards === undefined || cards.length === 0) return m;
    let cardChanged = false;
    const nextCards = cards.map((c) => {
      // Only settle cards STILL generating args — never a plain running
      // (executing) card that legitimately awaits its tool_result.
      if (c.argsStreaming !== true) return c;
      cardChanged = true;
      const nc: ChatToolCall = { ...c, status: "done" };
      // Derive a total elapsed for a generation-timed card that never got a
      // final result (parity with finalizeRunningToolCalls in messageCommit.ts).
      if (c.timedFromGeneration === true && c.totalMs === undefined) {
        if (typeof c.generationMs === "number") {
          nc.totalMs = c.generationMs;
        } else if (typeof c.generationStartedAt === "number") {
          nc.totalMs = Math.max(0, Date.now() - c.generationStartedAt);
        }
      }
      nc.argsStreaming = undefined;
      nc.argsCharCount = undefined;
      return nc;
    });
    if (!cardChanged) return m;
    changed = true;
    return { ...m, toolCalls: nextCards };
  });
  return changed ? next : null;
}

/** Match an in-flight `argsStreaming` tool card against an incoming
 *  `generating_args` progress frame OR a final `tool_call` frame. SHARED by
 *  both `handleGeneratingArgs` (progress) and `pairFinalToolCallWithStreamingCard`
 *  (final) so the two paths can never drift apart again (the prior bug: two
 *  hand-written `tryMatch` variants with subtly different conditions — one
 *  required the INCOMING frame's id to be absent for the name fallback, the
 *  other did not, and one idMatch branch did not gate on `argsStreaming`).
 *
 *  Invariants enforced here (fixes P0 + P1):
 *   - **Only `argsStreaming === true` cards ever match.** A card that the final
 *     `tool_call` frame already flipped into the normal running (executing)
 *     state has `argsStreaming` cleared, so a LATE progress frame carrying the
 *     same `tool_call_id` will NOT re-match it (P0: no flip-back to "正在生成
 *     参数…"). Both idMatch and nameMatch are gated on this.
 *   - **idMatch**: the incoming frame carries an id AND the card already holds
 *     that same id.
 *   - **nameMatch (id-less card fallback)**: the CARD has no id yet
 *     (`callId === undefined`) and the tool name + running state line up —
 *     REGARDLESS of whether THIS frame carries an id. This lets a later frame
 *     that finally assigns an id re-find the still-id-less card (P1: no second
 *     card) instead of spawning a duplicate; the caller then backfills the id.
 *  Scans newest-first and returns the matched index, or -1. */
function matchStreamingToolCard(
  calls: readonly ChatToolCall[],
  tool: string,
  incomingCallId: string | undefined,
): number {
  for (let i = calls.length - 1; i >= 0; i--) {
    const c = calls[i];
    // P0: only cards still in the args-streaming sub-state are eligible — a
    // card already flipped to the executing state must be immune to late
    // progress frames (even ones with a matching id).
    if (c === undefined || c.argsStreaming !== true) continue;
    const idMatch = incomingCallId !== undefined && c.callId === incomingCallId;
    // P1: id-less card fallback — pair by tool name + running when the card
    // never received an id (its `callId` is undefined), independent of whether
    // THIS frame now carries one.
    const nameMatch =
      c.callId === undefined && c.tool === tool && c.status === "running";
    if (idMatch || nameMatch) return i;
  }
  return -1;
}

/** When a final `tool_call` frame arrives, try to reuse an existing
 *  `argsStreaming` card (seeded earlier by `generating_args` progress frames)
 *  instead of creating a second card. Matches by upstream `tool_call_id`
 *  (primary) or a running argsStreaming card of the same tool name in the round
 *  (fallback, for providers whose progress frames lacked an id). On a hit it
 *  replaces the placeholder args with the consolidated `arguments`, clears
 *  `argsStreaming`/`argsCharCount`, and keeps `status: "running"`. Returns true
 *  when it consumed the frame; false to fall through to the normal push path. */
function pairFinalToolCallWithStreamingCard(
  tab: ChatTab,
  ctx: FrameHandlerContext,
  payload: Record<string, unknown>,
  tool: string,
  args: Record<string, unknown>,
): { pairedCardId: string } | null {
  const finalCallId =
    typeof payload.tool_call_id === "string" && payload.tool_call_id !== ""
      ? payload.tool_call_id
      : undefined;
  // Backend-authoritative generation duration (ms): time the model spent
  // streaming this tool_call's arguments (first generating_args → drain). When
  // present it marks the card as `timedFromGeneration` and is the canonical
  // input to `totalMs = generationMs + durationMs` (computed when the final
  // tool_result lands) — identical live and on reload, no Date.now() drift.
  const generationMs =
    typeof payload.generation_ms === "number" && payload.generation_ms >= 0
      ? payload.generation_ms
      : undefined;
  // Backend's TOOL_CALL frame stamp (ms epoch UTC); see `handleToolCall` for
  // why we mirror it onto the merged card too. The original seed (created by
  // an earlier `generating_args` progress frame) had no `ts` because progress
  // frames are not stamped by `_stamp_emitted_at` — only the final `tool_call`
  // frame carries it, so we backfill `ts` here.
  const tsFromFrame =
    typeof payload.emitted_at_ms === "number" && payload.emitted_at_ms > 0
      ? payload.emitted_at_ms
      : undefined;
  const ri = readRoundIndex(payload);
  const roundMsgId = ri !== null ? tab.roundMessageIds[ri] : undefined;
  const tryMatch = (calls: readonly ChatToolCall[]): number =>
    matchStreamingToolCard(calls, tool, finalCallId);
  const candidates: (string | null | undefined)[] = [
    roundMsgId,
    tab.activeToolMessageId,
  ];
  let found: { msgIdx: number; callIdx: number } | null = null;
  for (const mid of candidates) {
    if (mid === undefined || mid === null) continue;
    const idx = tab.messages.findIndex((m) => m.id === mid);
    if (idx >= 0) {
      const callIdx = tryMatch(tab.messages[idx]?.toolCalls ?? []);
      if (callIdx >= 0) {
        found = { msgIdx: idx, callIdx };
        break;
      }
    }
  }
  if (found === null) {
    for (let i = tab.messages.length - 1; i >= 0; i--) {
      const m = tab.messages[i];
      if (m === undefined) continue;
      const isStreaming =
        (m.meta as { streaming?: unknown } | undefined)?.streaming === true;
      if (!isStreaming || m.toolCalls === undefined) continue;
      const callIdx = tryMatch(m.toolCalls);
      if (callIdx >= 0) {
        found = { msgIdx: i, callIdx };
        break;
      }
    }
  }
  if (found === null) return null;
  const msg = tab.messages[found.msgIdx]!;
  const calls = [...(msg.toolCalls ?? [])];
  const c = calls[found.callIdx]!;
  // Replace placeholder args with the final consolidated arguments; flip out of
  // the args-streaming sub-state into the normal running (executing) state by
  // explicitly resetting `argsStreaming`/`argsCharCount` to undefined so the
  // card renders the real arguments preview, not the byte count.
  calls[found.callIdx] = {
    ...c,
    args,
    status: "running",
    argsStreaming: undefined,
    argsCharCount: undefined,
    ...(finalCallId !== undefined ? { callId: finalCallId } : {}),
    // Backfill `ts` from the final tool_call frame's `emitted_at_ms`. Don't
    // overwrite if the seed already had one (defensive — should not happen
    // because `generating_args` frames are unstamped, but explicit > implicit).
    ...(tsFromFrame !== undefined && c.ts === undefined
      ? { ts: tsFromFrame }
      : {}),
    // Store the backend-authoritative generation duration (ms) when the final
    // tool_call frame carries it; mark timedFromGeneration so the final result
    // computes totalMs = generationMs + durationMs (backend values, reload-safe).
    ...(generationMs !== undefined
      ? { generationMs, timedFromGeneration: true }
      : {}),
  };
  const nextMsg: ChatMessage = { ...msg, toolCalls: calls };
  ctx.patchTab({
    messages: [
      ...tab.messages.slice(0, found.msgIdx),
      nextMsg,
      ...tab.messages.slice(found.msgIdx + 1),
    ],
  });
  // Return the PAIRED card's id (preserved from the earlier `generating_args`
  // seed — not the final `tool_call` frame's id). Callers driving per-card
  // side-effects (todowrite task list, question pending pointer, etc.) must key
  // off THIS id so they match the rendered card and the message's `call.id`.
  return { pairedCardId: c.id };
}

const handleToolCall: FrameHandler = (tab, frame, ctx) => {
  const payload = asPayload(frame);
  if (payload === null) return;
  const tool = payload.tool_name;
  if (typeof tool !== "string" || tool === "") return;
  const rawArgs = payload.arguments;
  const args: Record<string, unknown> =
    rawArgs !== null && typeof rawArgs === "object"
      ? (rawArgs as Record<string, unknown>)
      : {};
  // Backend stamps every TOOL_CALL frame with `emitted_at_ms` (ms epoch UTC,
  // see streaming.py `_stamp_emitted_at`). Persist it onto the live ChatToolCall
  // as `ts` so it shares the SAME field the reload path already uses (see
  // `_streaming_helpers._card` writing `entry["ts"]=_real_ts` from the same
  // `emitted_at_ms`). This is the wall-clock anchor that lets ToolExecPanel
  // compute an elapsed timer that survives unmount→remount (route switch,
  // browser tab switch, v-if toggle): a stable absolute start time, not a
  // component-local `performance.now()` ref that resets on every mount —
  // which was the root cause of "切换浏览器标签后工具卡计时器归零" bug.
  const tsFromFrame =
    typeof payload.emitted_at_ms === "number" && payload.emitted_at_ms > 0
      ? payload.emitted_at_ms
      : undefined;
  const call: ChatToolCall = {
    id: frame.frame_id,
    ...(typeof payload.tool_call_id === "string" && payload.tool_call_id !== ""
      ? { callId: payload.tool_call_id }
      : {}),
    tool,
    args,
    status: "running",
    ...(tsFromFrame !== undefined ? { ts: tsFromFrame } : {}),
  };
  // ── Pair with an in-flight generating_args card (V2 enhancement) ──────────
  // If the model already surfaced a streaming "正在生成参数…" card for THIS call
  // (a prior `tool_result` frame with `phase === "generating_args"`), the final
  // `tool_call` frame must REUSE that same card — replace its placeholder args
  // with the consolidated `arguments`, clear `argsStreaming`, and keep it in the
  // normal running state — instead of pushing a SECOND card. Pairing key:
  // tool_call_id (primary); fallback: a running argsStreaming card of the same
  // tool name in the round (when the provider's progress frames lacked an id).
  const paired = pairFinalToolCallWithStreamingCard(tab, ctx, payload, tool, args);
  if (paired !== null) {
    // Still drive harness side-effects off the final frame's arguments. Key
    // them off the PAIRED card's id (preserved from the `generating_args` seed)
    // so the pending-question pointer + the rendered question card's frame id
    // stay in lock-step — otherwise `tab.pendingQuestion.frameId` would point
    // at the final frame id while the card uses the seed's id, the away
    // auto-answer countdown's `frameId === pendingQuestion.frameId` guard would
    // never match, and the same mismatch silently breaks `hasActiveQuestion`'s
    // `call.id === fid` filter that keeps the active question card visible
    // when tool messages are toggled off.
    if (tool === "todowrite") {
      applyTodoWrite(args, ctx);
    } else if (tool === "question") {
      applyQuestion(tab, paired.pairedCardId, args, ctx);
    }
    return;
  }
  // ── Harness control tools (V2 enhancement) ────────────────────────────────
  // `todowrite` / `question` still render a normal tool card (below), but they
  // also drive a dedicated UI surface off the SAME frame arguments:
  //   - todowrite → refresh the latest task list (`tab.todoList`, top bar);
  //   - question  → activate the in-conversation ChatQuestionCard (`tab.pendingQuestion`).
  // These side-effects are idempotent on the frame id so a re-delivered frame
  // does not double-apply.
  if (tool === "todowrite") {
    applyTodoWrite(args, ctx);
  } else if (tool === "question") {
    applyQuestion(tab, frame.frame_id, args, ctx);
  }
  // ── Round grouping (V1 useChat.js:2455-2520 parity) ───────────────────────
  // V1's chat is one ordered `messages` list. In the agentic loop every LLM
  // round pushes — in real arrival order — an `assistant{content: <round
  // lead-in>, tool_calls}` message followed by a live `tool_indicator` card.
  // There is NO "all text on top / all tool cards at the bottom" double track:
  // each round's thinking text sits directly above ITS round's tool cards.
  //
  // V2 mirrors this by pushing per-round assistant messages straight into
  // `tab.messages` while streaming (marked `meta.streaming:true` until the turn
  // settles), so the live layout already equals the reload layout (the backend
  // persists one assistant message per round — _streaming_helpers.py
  // build_tool_call_message, which now ALSO groups by `round_index`, making
  // stream↔reload byte-for-byte identical).
  //
  // Round membership is decided by the backend-stamped `round_index` (the LLM
  // call that issued this tool call) with ZERO inference: the round already
  // has a message ⇒ append this card onto it (parallel / batched calls, or a
  // later same-round tool after some narration); no message yet ⇒ open a new
  // per-round message carrying this round's lead-in text. The legacy
  // "lead_in non-empty ⇒ new round" heuristic (which mis-split a single LLM
  // call's `tool → narration → tool` sequence) is the fallback ONLY when
  // `round_index` is absent (old backend / non-agentic turns).
  const ri = readRoundIndex(payload);
  const leadInFromFrame =
    typeof payload.lead_in === "string" ? payload.lead_in : "";
  const leadInText =
    tab.streamingContent !== "" ? tab.streamingContent : leadInFromFrame;

  if (ri !== null) {
    // A tool_call for round `ri` proves every round < ri finished on the
    // backend; settle any card still stuck "生成参数中" in those earlier rounds
    // (its own completion frames were lost) so it stops spinning. Apply the
    // settled messages as this handler's working base so the round-append
    // patches below build on top of it (a later patch would otherwise clobber
    // the fix). Exclude round `ri`'s own message so a freshly-seeded live card
    // for THIS round (possibly orphan-untagged) is never settled prematurely.
    const settled = settleStalePriorRoundCards(
      tab.messages,
      ri,
      tab.roundMessageIds[ri],
    );
    if (settled !== null) {
      ctx.patchTab({ messages: settled });
      tab = { ...tab, messages: settled };
    }
    const openId = tab.roundMessageIds[ri];
    const openIdx =
      openId !== undefined
        ? tab.messages.findIndex((m) => m.id === openId)
        : -1;
    if (openIdx >= 0) {
      // Round already open → append this card. Fold any pending lead-in/
      // narration text (`streamingContent`) into the round's content so
      // same-round text that surfaced before this card stays bound to its
      // round (not leaked to the next round). The card sits below the text.
      const target = tab.messages[openIdx]!;
      // Dedup against an orphan-partial seed: a streaming tool (exec) whose
      // FIRST partial tool_result arrived BEFORE this `tool_call` frame already
      // opened this round message + seeded a running card carrying the same
      // `tool_call_id` (see handleToolResult's orphan-partial round-open path).
      // Reuse that card (backfill the consolidated args) instead of pushing a
      // SECOND duplicate card for the same call.
      const incomingCallId =
        typeof payload.tool_call_id === "string" && payload.tool_call_id !== ""
          ? payload.tool_call_id
          : undefined;
      const existingCards = target.toolCalls ?? [];
      const dupIdx =
        incomingCallId !== undefined
          ? existingCards.findIndex((c) => c.callId === incomingCallId)
          : -1;
      // Backfill this round's `request_id` if the opening frame lacked it but
      // a later same-round frame carries it (defensive — the backend stamps
      // every round frame, so normally the round message already has it).
      const appendReqId = readRequestId(payload);
      const hasReqId =
        typeof (target.meta as Record<string, unknown> | undefined)?.[
          "request_id"
        ] === "string" &&
        (target.meta as Record<string, unknown>)["request_id"] !== "";
      const nextCards =
        dupIdx >= 0
          ? existingCards.map((c, i) =>
              i === dupIdx
                ? {
                    ...c,
                    // Backfill the consolidated args + the tool_call frame's
                    // ts/generation fields; keep the seeded card's live output
                    // + running status.
                    args,
                    ...(tsFromFrame !== undefined && c.ts === undefined
                      ? { ts: tsFromFrame }
                      : {}),
                  }
                : c,
            )
          : [...existingCards, call];
      const nextMsg: ChatMessage = {
        ...target,
        ...(tab.streamingContent !== ""
          ? { content: target.content + tab.streamingContent }
          : {}),
        toolCalls: nextCards,
        ...(!hasReqId && appendReqId !== ""
          ? { meta: { ...(target.meta ?? {}), request_id: appendReqId } }
          : {}),
      };
      const patch: Partial<ChatTab> = {
        messages: [
          ...tab.messages.slice(0, openIdx),
          nextMsg,
          ...tab.messages.slice(openIdx + 1),
        ],
        activeToolMessageId: target.id,
      };
      if (tab.streamingContent !== "") patch.streamingContent = "";
      ctx.patchTab(patch);
      return;
    }
    // New round → new per-round assistant message carrying this round's
    // lead-in text + its first tool card.
    const id = ctx.nextMessageId();
    // Per-round prompt-snapshot id (V1 parity): the tool_call frame carries
    // its round's `request_id`; stamp it onto the round message's
    // `meta.request_id` so this round's tool cards' 📄 buttons open ITS
    // round's prompt live (and persistence stamps the same — stream↔reload
    // identical). Empty → no `request_id` key (no 📄 button, as before).
    const reqId = readRequestId(payload);
    const roundMsg: ChatMessage = {
      id,
      role: "assistant",
      content: leadInText,
      createdAt: Date.now(),
      ...(tab.conversationId !== null
        ? { conversationId: tab.conversationId }
        : {}),
      toolCalls: [call],
      ...senderFields(tab, payload),
      ...roundModelFields(tab),
      meta: {
        streaming: true,
        roundIndex: ri,
        ...(reqId !== "" ? { request_id: reqId } : {}),
      },
    };
    const patch: Partial<ChatTab> = {
      messages: [...tab.messages, roundMsg],
      activeToolMessageId: id,
      roundMessageIds: { ...tab.roundMessageIds, [ri]: id },
    };
    // Clear the live buffer (the lead-in is now committed to its round
    // message): the NEXT round refills an empty buffer.
    if (tab.streamingContent !== "") patch.streamingContent = "";
    ctx.patchTab(patch);
    return;
  }

  // ── Legacy fallback (no round_index): lead_in-boundary heuristic ──────────
  const activeId = tab.activeToolMessageId;
  const activeIdx =
    activeId !== null
      ? tab.messages.findIndex((m) => m.id === activeId)
      : -1;
  const openNewRound = activeIdx < 0 || leadInText !== "";

  if (openNewRound) {
    // New round → new per-round assistant message carrying this round's
    // lead-in text + its first tool card. Stays `meta.streaming:true` until
    // the turn settles (then running cards are finalized + the marker dropped).
    const id = ctx.nextMessageId();
    const roundMsg: ChatMessage = {
      id,
      role: "assistant",
      content: leadInText,
      createdAt: Date.now(),
      ...(tab.conversationId !== null
        ? { conversationId: tab.conversationId }
        : {}),
      toolCalls: [call],
      ...senderFields(tab, payload),
      ...roundModelFields(tab),
      meta: { streaming: true },
    };
    const patch: Partial<ChatTab> = {
      messages: [...tab.messages, roundMsg],
      activeToolMessageId: id,
    };
    // Clear the live buffer (the lead-in is now committed to its round
    // message): the NEXT round refills an empty buffer, so a re-emitted
    // lead-in lands as a *separate* round message rather than concatenating
    // twice into one bubble (anti-"文本重复两遍" — chatTabs.spec.ts).
    if (tab.streamingContent !== "") {
      patch.streamingContent = "";
    }
    ctx.patchTab(patch);
    return;
  }

  // Same round (parallel / batched tool_call, no fresh lead-in) → append onto
  // the active round message's tool cards.
  const target = tab.messages[activeIdx];
  if (target === undefined) return;
  const nextMsg: ChatMessage = {
    ...target,
    toolCalls: [...(target.toolCalls ?? []), call],
  };
  ctx.patchTab({
    messages: [
      ...tab.messages.slice(0, activeIdx),
      nextMsg,
      ...tab.messages.slice(activeIdx + 1),
    ],
  });
};

/** Locate, among a tab's streaming messages, the index of the message holding
 *  a tool card matching `callId` (primary) or a most-recent running card of
 *  `tool` name (fallback). Prefer the round message named by `preferMsgId`
 *  (the backend `round_index`'s message — exact, zero-inference), then the
 *  active round message, then scan other streaming messages newest-first.
 *  Returns `{ msgIdx, callIdx }` or null. */
function findToolCardSlot(
  tab: ChatTab,
  tool: string,
  callId: string | undefined,
  preferMsgId?: string,
): { msgIdx: number; callIdx: number } | null {
  const tryMatch = (calls: readonly ChatToolCall[]): number => {
    for (let i = calls.length - 1; i >= 0; i--) {
      const c = calls[i];
      if (c === undefined) continue;
      const idMatch = callId !== undefined && c.callId === callId;
      const nameMatch =
        callId === undefined && c.tool === tool && c.status === "running";
      if (idMatch || nameMatch) return i;
    }
    return -1;
  };
  // Exact path: the round message named by the result's `round_index` (the
  // result shares its call's round — backend-stamped, no inference).
  if (preferMsgId !== undefined) {
    const idx = tab.messages.findIndex((m) => m.id === preferMsgId);
    if (idx >= 0) {
      const callIdx = tryMatch(tab.messages[idx]?.toolCalls ?? []);
      if (callIdx >= 0) return { msgIdx: idx, callIdx };
    }
  }
  // Fast path: the active round message (results normally land on the round
  // whose message is currently open).
  const activeId = tab.activeToolMessageId;
  if (activeId !== null && activeId !== preferMsgId) {
    const idx = tab.messages.findIndex((m) => m.id === activeId);
    if (idx >= 0) {
      const callIdx = tryMatch(tab.messages[idx]?.toolCalls ?? []);
      if (callIdx >= 0) return { msgIdx: idx, callIdx };
    }
  }
  // Fallback: scan all streaming messages newest-first (handles a late result
  // arriving after a new round already opened, or a no-active-message orphan).
  for (let i = tab.messages.length - 1; i >= 0; i--) {
    const m = tab.messages[i];
    if (m === undefined) continue;
    const isStreaming =
      (m.meta as { streaming?: unknown } | undefined)?.streaming === true;
    if (!isStreaming || m.toolCalls === undefined) continue;
    const callIdx = tryMatch(m.toolCalls);
    if (callIdx >= 0) return { msgIdx: i, callIdx };
  }
  return null;
}

/** Loose fallback for a FINAL `tool_result` whose strict `findToolCardSlot`
 *  pairing missed (问题2): the result carries an id that does not line up with
 *  any card's `callId` (a broken generating_args → tool_call → tool_result id
 *  chain), yet a same-named card is still sitting `running`. State-Truth-First:
 *  a real terminal frame MUST move its card to a terminal state immediately —
 *  never get dropped and leave the card spinning until the turn ends (which is
 *  what produced "card still running while the next text block already shows").
 *  Scans all streaming messages newest-first for ANY `running` card of `tool`
 *  (ignoring callId), preferring the round/active message. Returns the slot or
 *  null when truly none is running. */
function findRunningCardByName(
  tab: ChatTab,
  tool: string,
  preferMsgId?: string,
): { msgIdx: number; callIdx: number } | null {
  // Conservative fallback for a final tool_result whose id-based pairing
  // (`findToolCardSlot`) missed (e.g. a broken generating_args→tool_call→
  // tool_result id chain).  To avoid mis-pairing under concurrent same-name
  // tools — which would prematurely mark the WRONG card done and splice this
  // result's output onto it — we:
  //   * EXCLUDE `argsStreaming` cards (a tool still generating its arguments
  //     must not be flipped done by another tool's result); and
  //   * only match when the executing same-name running card is UNIQUE across
  //     the whole tab.  If two or more same-name cards are running we return
  //     null (let the orphan branch / end-of-turn `finalizeRunningToolCalls`
  //     handle it) rather than guess and cross the streams.
  const candidates: { msgIdx: number; callIdx: number }[] = [];
  for (let i = 0; i < tab.messages.length; i++) {
    const m = tab.messages[i];
    if (m === undefined || m.toolCalls === undefined) continue;
    for (let j = 0; j < m.toolCalls.length; j++) {
      const c = m.toolCalls[j];
      if (c === undefined) continue;
      if (
        c.tool === tool &&
        c.status === "running" &&
        c.argsStreaming !== true
      ) {
        candidates.push({ msgIdx: i, callIdx: j });
      }
    }
  }
  if (candidates.length !== 1) return null;
  // Prefer the result's round / active message when it coincides (defensive —
  // with a unique candidate this is just a sanity preference, not required).
  const only = candidates[0]!;
  void preferMsgId;
  return only;
}

/** Open a brand-new per-round assistant message (carrying the live lead-in
 *  text, if any) so an early tool card has a round to live on, mirroring the
 *  "new round" branch of `handleToolCall`. Returns the new message's id (and
 *  the patched messages list) so the caller can append cards onto it. Used by
 *  the `generating_args` progress path when the model starts streaming a long
 *  tool call BEFORE any `tool_call` frame has opened the round. */
function openRoundMessage(
  tab: ChatTab,
  ri: number | null,
  ctx: FrameHandlerContext,
): { id: string; messages: ChatMessage[]; patch: Partial<ChatTab> } {
  const id = ctx.nextMessageId();
  const leadInText = tab.streamingContent;
  const roundMsg: ChatMessage = {
    id,
    role: "assistant",
    content: leadInText,
    createdAt: Date.now(),
    ...(tab.conversationId !== null
      ? { conversationId: tab.conversationId }
      : {}),
    toolCalls: [],
    meta: {
      streaming: true,
      ...(ri !== null ? { roundIndex: ri } : {}),
    },
  };
  const messages = [...tab.messages, roundMsg];
  const patch: Partial<ChatTab> = {
    activeToolMessageId: id,
    ...(ri !== null
      ? { roundMessageIds: { ...tab.roundMessageIds, [ri]: id } }
      : {}),
  };
  if (tab.streamingContent !== "") patch.streamingContent = "";
  return { id, messages, patch };
}

/** Handle a throttled `generating_args` progress frame (V2 enhancement,
 *  carried on the existing `tool_result` frame_type discriminated by
 *  `phase === "generating_args"` so the locked 13-value StreamFrameType enum
 *  (§3.1) is reused, not extended).
 *
 *  These frames stream WHILE the model is still emitting a long tool call's
 *  `arguments` (e.g. a big `write`/`edit` body) and carry the cumulative args
 *  text in `result` plus the latest chunk in `delta`. We surface a tool card
 *  EARLY in an `argsStreaming` running sub-state showing the accumulated
 *  character count, then `handleToolCall` flips it to the normal running state
 *  when the final `tool_call` frame (same `tool_call_id`) lands.
 *
 *  Pairing: prefer the upstream `tool_call_id`; when the provider has not yet
 *  emitted one (first chunk → null), fall back to "a running argsStreaming card
 *  of the same tool name in the round" so successive progress frames update the
 *  same card instead of spawning duplicates. Returns true when it consumed the
 *  frame (caller returns immediately); false to fall through to the normal
 *  tool_result logic. */
function handleGeneratingArgs(
  tab: ChatTab,
  frame: ChatStreamFrame,
  ctx: FrameHandlerContext,
  payload: Record<string, unknown>,
  tool: string,
): boolean {
  if (payload.phase !== "generating_args") return false;
  // Cumulative char count: prefer the full accumulated `result` length, else
  // the latest `delta` (a single chunk — best effort when `result` absent).
  const resultStr =
    typeof payload.result === "string" ? payload.result : undefined;
  const deltaStr =
    typeof payload.delta === "string" ? payload.delta : undefined;
  const partialCallId =
    typeof payload.tool_call_id === "string" && payload.tool_call_id !== ""
      ? payload.tool_call_id
      : undefined;
  const ri = readRoundIndex(payload);
  const roundMsgId = ri !== null ? tab.roundMessageIds[ri] : undefined;

  // Locate an existing argsStreaming card to update. Primary key: tool_call_id;
  // fallback: same-name running argsStreaming card in the round/active message.
  // Both branches go through the SHARED `matchStreamingToolCard` helper so the
  // progress path and the final-pairing path can never drift apart again, and
  // so an already-flipped (executing) card is immune to a late progress frame
  // even when that frame carries the same id (P0).
  const findStreamingSlot = (): { msgIdx: number; callIdx: number } | null => {
    const tryMatch = (calls: readonly ChatToolCall[]): number =>
      matchStreamingToolCard(calls, tool, partialCallId);
    const candidates: (string | null | undefined)[] = [
      roundMsgId,
      tab.activeToolMessageId,
    ];
    for (const mid of candidates) {
      if (mid === undefined || mid === null) continue;
      const idx = tab.messages.findIndex((m) => m.id === mid);
      if (idx >= 0) {
        const callIdx = tryMatch(tab.messages[idx]?.toolCalls ?? []);
        if (callIdx >= 0) return { msgIdx: idx, callIdx };
      }
    }
    // Newest-first scan of streaming messages (late/orphan frames).
    for (let i = tab.messages.length - 1; i >= 0; i--) {
      const m = tab.messages[i];
      if (m === undefined) continue;
      const isStreaming =
        (m.meta as { streaming?: unknown } | undefined)?.streaming === true;
      if (!isStreaming || m.toolCalls === undefined) continue;
      const callIdx = tryMatch(m.toolCalls);
      if (callIdx >= 0) return { msgIdx: i, callIdx };
    }
    return null;
  };

  const slot = findStreamingSlot();
  if (slot !== null) {
    // Update the existing card's accumulated char count (keep argsStreaming).
    const msg = tab.messages[slot.msgIdx]!;
    const calls = [...(msg.toolCalls ?? [])];
    const c = calls[slot.callIdx]!;
    const nextCount =
      resultStr !== undefined
        ? resultStr.length
        : (c.argsCharCount ?? 0) + (deltaStr?.length ?? 0);
    calls[slot.callIdx] = {
      ...c,
      argsStreaming: true,
      argsCharCount: nextCount,
      // Backfill the upstream call id if a later frame finally carries one
      // (provider's first chunk may have had null) — so the final tool_call
      // frame can still pair by id.
      ...(c.callId === undefined && partialCallId !== undefined
        ? { callId: partialCallId }
        : {}),
    };
    const nextMsg: ChatMessage = { ...msg, toolCalls: calls };
    ctx.patchTab({
      messages: [
        ...tab.messages.slice(0, slot.msgIdx),
        nextMsg,
        ...tab.messages.slice(slot.msgIdx + 1),
      ],
    });
    return true;
  }

  // No card yet → seed a NEW running argsStreaming card. Find/open the round
  // message (parallel to handleToolCall's round-opening logic).
  //
  // P0 guard: a LATE/duplicate progress frame for a call that already has a
  // card which is NO LONGER args-streaming (the final `tool_call` frame already
  // flipped it to executing, or a `tool_result` already settled it) must NOT
  // seed a second card. `findStreamingSlot` only matches `argsStreaming` cards,
  // so a flipped card returns null here; without this guard we'd spawn a
  // duplicate. We only seed when the call id is NOT already represented by any
  // existing card in the tab. (When the frame carries no id we can't dedupe by
  // id, so we still seed — first-frame / id-less providers behave as before.)
  if (partialCallId !== undefined) {
    const alreadyHasCard = tab.messages.some((m) =>
      (m.toolCalls ?? []).some((c) => c.callId === partialCallId),
    );
    if (alreadyHasCard) return true;
  }
  const initialCount =
    resultStr !== undefined ? resultStr.length : (deltaStr?.length ?? 0);
  const seeded: ChatToolCall = {
    id: frame.frame_id,
    ...(partialCallId !== undefined ? { callId: partialCallId } : {}),
    tool,
    args: {},
    status: "running",
    argsStreaming: true,
    argsCharCount: initialCount,
    // Mark this card as "timed from generation" and stamp the generation start
    // so the final-result handler can report TOTAL (generation + execution)
    // wall-clock rather than the execution-only backend duration (问题1).
    timedFromGeneration: true,
    generationStartedAt: Date.now(),
  };
  // Target an existing round/active message if present; otherwise open one.
  // Round-ordering guard (parity with handleToolResult orphan-partial path,
  // line ~1280): when this generating_args frame carries an explicit
  // `round_index` whose round message has NOT opened yet (`roundMsgId ===
  // undefined`), we must NOT seed it onto the PRIOR round's
  // `activeToolMessageId` — that mis-attributes a later round's tool card to
  // an earlier round's bubble, causing the "tool call panel displayed under
  // the wrong message during streaming" bug. Instead open this round's own
  // message so the card lands in its correct position.
  let targetIdx = -1;
  if (roundMsgId !== undefined) {
    targetIdx = tab.messages.findIndex((m) => m.id === roundMsgId);
  }
  if (targetIdx < 0 && ri !== null && roundMsgId === undefined) {
    // New round whose message hasn't been opened yet — open it now rather
    // than falling back to the previous round's activeToolMessageId.
    const { id, messages, patch } = openRoundMessage(tab, ri, ctx);
    const newIdx = messages.findIndex((m) => m.id === id);
    const target = messages[newIdx]!;
    messages[newIdx] = { ...target, toolCalls: [seeded] };
    ctx.patchTab({ messages, ...patch });
    return true;
  }
  if (targetIdx < 0 && tab.activeToolMessageId !== null) {
    targetIdx = tab.messages.findIndex(
      (m) => m.id === tab.activeToolMessageId,
    );
  }
  if (targetIdx >= 0) {
    const target = tab.messages[targetIdx]!;
    const nextMsg: ChatMessage = {
      ...target,
      toolCalls: [...(target.toolCalls ?? []), seeded],
    };
    ctx.patchTab({
      messages: [
        ...tab.messages.slice(0, targetIdx),
        nextMsg,
        ...tab.messages.slice(targetIdx + 1),
      ],
    });
    return true;
  }
  // No round open yet → open a fresh per-round message holding the seed card.
  const { id, messages, patch } = openRoundMessage(tab, ri, ctx);
  const newIdx = messages.findIndex((m) => m.id === id);
  const target = messages[newIdx]!;
  messages[newIdx] = { ...target, toolCalls: [seeded] };
  ctx.patchTab({ messages, ...patch });
  return true;
}

const handleToolResult: FrameHandler = (tab, frame, ctx) => {
  const payload = asPayload(frame);
  if (payload === null) return;
  const tool = payload.tool_name;
  if (typeof tool !== "string") return;

  // ── generating_args progress frames (V2 enhancement) ──────────────────────
  // A `tool_result` frame with `phase === "generating_args"` is NOT a real tool
  // result — it is a throttled progress frame emitted while the model is still
  // streaming a long tool call's arguments. Route it to the dedicated handler
  // (early streaming card) and stop; ordinary partial/final tool_result frames
  // (no `phase`) continue through the existing logic untouched.
  if (handleGeneratingArgs(tab, frame, ctx, payload, tool)) return;

  // WIRE-tools (V1 backend/tools/_exec.py:1010 + useChat.js:1041 parity):
  // a streaming-capable tool (exec) emits several `tool_result` frames —
  // `partial=true` increments carrying a `delta` chunk, then a final
  // `partial=false`/absent frame with the consolidated result + size /
  // truncated metadata. The 13-value `StreamFrameType` enum is locked
  // (§3.1), so the increment is the same `tool_result` type discriminated
  // by the `partial` flag rather than a new frame kind.
  //
  // Single-track model: results update the matching tool card IN its round's
  // `messages` entry (bound under that round's lead-in), not a separate
  // bottom-of-stream buffer. Pairing key: tool_call_id (primary) so parallel
  // same-named tools (two `exec` in one round) bind correctly; tool name +
  // running (fallback) for the local XML protocol.
  //
  // Zero-inference round targeting: a result shares its CALL's `round_index`
  // (backend-stamped), so we resolve the exact round message via
  // `roundMessageIds[round_index]` and prefer it when pairing — robust even
  // when a later round already opened (a slow tool's result arriving after the
  // next round started no longer mis-binds to the wrong round's card).
  const ri = readRoundIndex(payload);
  const roundMsgId =
    ri !== null ? tab.roundMessageIds[ri] : undefined;
  // Index of the round message (orphan-fallback target): prefer the exact
  // round message, else the active round message.
  const roundOrActiveIdx = (() => {
    if (roundMsgId !== undefined) {
      const i = tab.messages.findIndex((m) => m.id === roundMsgId);
      if (i >= 0) return i;
    }
    const activeId = tab.activeToolMessageId;
    return activeId !== null
      ? tab.messages.findIndex((m) => m.id === activeId)
      : -1;
  })();
  const isPartial = payload.partial === true;
  if (isPartial) {
    const deltaRaw = payload.delta;
    const delta =
      typeof deltaRaw === "string"
        ? deltaRaw
        : stringifyToolResult(payload.result);
    if (delta === "") return;
    const partialCallId =
      typeof payload.tool_call_id === "string" && payload.tool_call_id !== ""
        ? payload.tool_call_id
        : undefined;
    const slot = findToolCardSlot(tab, tool, partialCallId, roundMsgId);
    if (slot === null) {
      // Orphan partial (no preceding tool_call observed). Seed a running card.
      //
      // Round-ordering guard (fix for "later round's still-running tool card
      // rendered ABOVE an earlier round's completed card"): when this partial
      // carries an explicit `round_index` whose round message has NOT opened
      // yet (`roundMsgId === undefined`), we must NOT seed it onto the PRIOR
      // round's `activeToolMessageId` — that mis-attributes a later round's
      // tool to an earlier round's bubble and, because messages render in array
      // order, paints the later-round (often slow, still-running) card ABOVE the
      // earlier round's content. Instead OPEN this round's own message at the
      // tail (mirroring handleToolCall / handleChunk's round-open path) so the
      // card lands in its correct round, after the earlier rounds.
      let activeIdx: number;
      let baseMessages = tab.messages;
      let extraPatch: Partial<ChatTab> = {};
      if (ri !== null && roundMsgId === undefined) {
        const opened = openRoundMessage(tab, ri, ctx);
        baseMessages = opened.messages;
        extraPatch = opened.patch;
        activeIdx = baseMessages.findIndex((m) => m.id === opened.id);
      } else {
        // ri is null (legacy / no round info) OR the round message exists but
        // held no matching card — fall back to the round/active message.
        activeIdx = roundOrActiveIdx;
      }
      if (activeIdx < 0) return;
      const target = baseMessages[activeIdx];
      if (target === undefined) return;
      // BOUNDED preview from the first delta (head+tail; never the raw stream).
      const cardKey = partialCallId ?? frame.frame_id;
      const preview = ctx.bufferToolOutput(cardKey, delta);
      const seeded: ChatToolCall = {
        id: frame.frame_id,
        ...(partialCallId !== undefined ? { callId: partialCallId } : {}),
        tool,
        args: {},
        output: preview,
        status: "running",
      };
      const nextMsg: ChatMessage = {
        ...target,
        toolCalls: [...(target.toolCalls ?? []), seeded],
      };
      // The card must appear now (one paint); subsequent deltas are throttled.
      ctx.patchTab({
        ...extraPatch,
        messages: [
          ...baseMessages.slice(0, activeIdx),
          nextMsg,
          ...baseMessages.slice(activeIdx + 1),
        ],
      });
      return;
    }
    // Existing card: absorb the delta into its BOUNDED preview buffer and let
    // the THROTTLE write the rendered preview into `output` (at most one paint
    // per animation frame), instead of an O(n) concat + full messages-array
    // replacement per frame. No reactive write here — the buffer holds the data
    // and `flushToolOutput` (on the throttle) patches the card.
    const msg = tab.messages[slot.msgIdx]!;
    const c = (msg.toolCalls ?? [])[slot.callIdx]!;
    const cardKey = c.callId ?? c.id;
    ctx.bufferToolOutput(cardKey, delta);
    return;
  }

  const output = stringifyToolResult(payload.result);
  const isError = isToolErrorOutput(output);
  // Per-call cancel (§3.1 appended field): a terminal result synthesized
  // because the user stopped THIS one tool. Treated as a (non-fatal) settled
  // card — it stops the spinner and shows the "[已取消]/[cancelled]" text the
  // backend put in ``result`` (same text is fed back to the model). We also
  // carry a ``cancelled`` flag on the card for optional distinct styling.
  const cancelled =
    typeof payload.cancelled === "boolean" ? payload.cancelled : undefined;
  // Appended size/truncated fields (backend StreamFrame.tool_result
  // §3.1) — optional; absent on older frames.
  const outputSize =
    typeof payload.size === "number" ? payload.size : undefined;
  const truncated =
    typeof payload.truncated === "boolean" ? payload.truncated : undefined;
  // Tool wall-clock run time (ms) — appended final-frame field (§3.1).
  const durationMs =
    typeof payload.duration_ms === "number" ? payload.duration_ms : undefined;
  const resultCallId =
    typeof payload.tool_call_id === "string" && payload.tool_call_id !== ""
      ? payload.tool_call_id
      : undefined;
  // Strict pairing first (id primary, id-less name fallback). When it misses,
  // fall back to ANY same-named running card (问题2: broken id chain) so a real
  // terminal frame always settles its card now, never leaves it spinning.
  const slot =
    findToolCardSlot(tab, tool, resultCallId, roundMsgId) ??
    findRunningCardByName(tab, tool, roundMsgId);
  if (slot === null) {
    // Truly orphan result (no preceding tool_call frame AND no running card) —
    // surface it as a completed card. Same round-ordering guard as the partial
    // orphan branch above: when this result carries an explicit `round_index`
    // whose round message has NOT opened yet, OPEN that round's own message at
    // the tail rather than mis-attributing the card onto a PRIOR round's
    // `activeToolMessageId` (which would render it out of round order).
    let activeIdx: number;
    let baseMessages = tab.messages;
    let extraPatch: Partial<ChatTab> = {};
    if (ri !== null && roundMsgId === undefined) {
      const opened = openRoundMessage(tab, ri, ctx);
      baseMessages = opened.messages;
      extraPatch = opened.patch;
      activeIdx = baseMessages.findIndex((m) => m.id === opened.id);
    } else {
      activeIdx = roundOrActiveIdx;
    }
    if (activeIdx < 0) return;
    const target = baseMessages[activeIdx];
    if (target === undefined) return;
    const orphan: ChatToolCall = {
      id: frame.frame_id,
      tool,
      args: {},
      output: renderBoundedPreview(output),
      status: isError ? "error" : "done",
      isError,
      ...(cancelled !== undefined ? { cancelled } : {}),
      outputSize,
      truncated,
      ...(durationMs !== undefined ? { durationMs } : {}),
    };
    const nextMsg: ChatMessage = {
      ...target,
      toolCalls: [...(target.toolCalls ?? []), orphan],
    };
    ctx.patchTab({
      ...extraPatch,
      messages: [
        ...baseMessages.slice(0, activeIdx),
        nextMsg,
        ...baseMessages.slice(activeIdx + 1),
      ],
    });
    return;
  }
  const msg = tab.messages[slot.msgIdx]!;
  const calls = [...(msg.toolCalls ?? [])];
  const c = calls[slot.callIdx]!;
  // Terminal frame: drop this card's live preview buffer so a pending throttled
  // flush can never resurrect stale live text over the settled output, and a
  // reused card key starts fresh next turn. (This frame writes the canonical
  // output below; the buffer is now obsolete.)
  ctx.flushToolOutput(c.callId ?? c.id);
  // 问题1: a card seeded from `generating_args` reports the TOTAL wall-clock of
  // "generation + execution", not the backend execution-only `duration_ms`.
  // AUTHORITATIVE source: backend `generation_ms` (stored on the card as
  // `generationMs` by the flip) + this frame's `duration_ms` — both backend
  // values, so the result is identical live and on reload (no Date.now() drift,
  // and survives persistence). FALLBACK (no generation_ms emitted): the
  // front-end `Date.now() - generationStartedAt` approximation.
  const totalMs = ((): number | undefined => {
    if (c.timedFromGeneration !== true) return undefined;
    if (typeof c.generationMs === "number") {
      return c.generationMs + (durationMs ?? 0);
    }
    if (typeof c.generationStartedAt === "number") {
      return Math.max(0, Date.now() - c.generationStartedAt);
    }
    return undefined;
  })();
  calls[slot.callIdx] = {
    ...c,
    // Final frame carries the consolidated result; replace any accumulated
    // partial buffer so the card shows the canonical output rather than the
    // live stream. Bounded defensively (head+tail) so the card never holds a
    // huge string even if the backend sends the full untruncated result — the
    // complete output is retrievable via the `read` tool.
    output: renderBoundedPreview(output),
    status: isError ? "error" : "done",
    isError,
    ...(cancelled !== undefined ? { cancelled } : {}),
    outputSize,
    truncated,
    // If the final result lands while the card is still in the args-streaming
    // sub-state (the `tool_call` frame that normally flips it was also lost),
    // exit that sub-state so it renders as a settled card, not "正在生成参数…".
    ...(c.argsStreaming ? { argsStreaming: undefined, argsCharCount: undefined } : {}),
    ...(durationMs !== undefined ? { durationMs } : {}),
    ...(totalMs !== undefined ? { totalMs } : {}),
  };
  const nextMsg: ChatMessage = { ...msg, toolCalls: calls };
  ctx.patchTab({
    messages: [
      ...tab.messages.slice(0, slot.msgIdx),
      nextMsg,
      ...tab.messages.slice(slot.msgIdx + 1),
    ],
  });
};

const handleEnd: FrameHandler = (_tab, frame, ctx) => {
  const payload = asPayload(frame);
  if (payload === null) return;
  const usage = payload.usage;
  if (usage !== null && typeof usage === "object") {
    ctx.patchTab({ streamingUsage: usage as ChatMessageUsage });
  }
  // V1 parity (backend/main.py:6716-6720): the terminal end frame carries
  // `request_id` when a prompt snapshot was saved so the frontend can
  // surface the "Prompt Snapshot" button on the assistant message. The
  // backend emits a supplementary end frame (reason="snapshot") after the
  // LLM's own end frame; we capture the id here and write it onto the
  // committed message in `confirmDone`.
  const rid = payload.request_id;
  if (typeof rid === "string" && rid !== "") {
    ctx.patchTab({ streamingRequestId: rid });
  }
  // Per-conversation TOKEN budget (max_budget_tokens): when the cap is hit the
  // backend stops the turn and stamps the terminal END frame with
  // `reason: "budget_exceeded"` (same pattern as the `reason: "snapshot"`
  // supplementary END above). The store has no i18n / toast instance, so we
  // raise a monotonic transient signal on the tab; the composer (setup context)
  // watches it to raise a warning toast + refresh the budget snapshot so the
  // badge shows the exhausted state. `Date.now()` keeps it strictly increasing
  // so a repeat hit still triggers the watcher.
  if (payload.reason === "budget_exceeded") {
    // Tail-appended decision metadata (may be absent on a legacy END): current
    // usage, the cap that was hit, and the cap a "continue" would apply
    // (current + raise_pct%). The composer reads this to render the
    // continue/stop dialog.
    const _used = payload.budget_used_tokens;
    const _max = payload.budget_max_tokens;
    const _next = payload.budget_next_max_tokens;
    const _pct = payload.budget_raise_pct;
    const decision =
      typeof _used === "number" &&
      typeof _max === "number" &&
      typeof _next === "number" &&
      typeof _pct === "number"
        ? { used: _used, max: _max, nextMax: _next, raisePct: _pct }
        : undefined;
    ctx.patchTab({
      budgetExceededSignal: Date.now(),
      ...(decision !== undefined ? { budgetDecision: decision } : {}),
    });
  }
};

const handleCompactionProgress: FrameHandler = (_tab, frame, ctx) => {
  // V2 enhancement — the backend is compacting (compressing) this
  // conversation's context and the model call is delayed by it. The backend
  // emits this frame ONLY when compaction is slow enough to notice (≈2s+,
  // typically when the Level 2 LLM summary runs).
  //
  // Backend payload shape (qai/chat/domain/stream_frame.py):
  //   { state: "compressing" | "done", message?: str }
  //
  // We flip a transient tab-level `compacting` flag; ChatMessageList renders a
  // passive "compressing context…" status banner while it is set and clears it
  // on `done`. No native dialog (AGENTS.md §3.9.2) — an inline banner only.
  const payload = asPayload(frame);
  if (payload === null) return;
  const state = payload.state;
  if (state === "compressing") {
    ctx.patchTab({ compacting: true });
  } else if (state === "done") {
    ctx.patchTab({ compacting: false });
  }
};

const handleContextUsage: FrameHandler = (_tab, frame, ctx) => {
  // Main-agent turn-internal LIVE context refresh (V2 enhancement; mirror of
  // the sub-agent per-round `used_tokens` refresh). The backend emits this
  // frame at each agentic ROUND boundary inside ONE turn with the
  // round-just-completed's PROVIDER-MEASURED wire size (State-Truth-First, NOT
  // an estimate) so the main-conversation context badge tracks the real wire
  // growth (e.g. 33K → 70K) WHILE a long multi-round tool turn runs, instead of
  // staying frozen at the prior turn's value until the turn-boundary
  // `GET /context` re-fetch.
  //
  // Backend payload shape (qai/chat/domain/stream_frame.py context_usage):
  //   { used_tokens: number, context_limit: number }
  //
  // We patch transient tab-level live fields; `useComposerCtxBadge` prefers
  // them over the `/context` value for an ordinary chat tab WHILE they are set.
  // Lifecycle: set here per round; CLEARED on the next `GET /context` refresh
  // (turn-boundary streaming→idle, owned by `useComposerCtxBadge.refreshCtx`)
  // so the authoritative probe value overrides the stale live value
  // (State-Truth-First 铁律 3: optimistic feedback + probe override).
  const payload = asPayload(frame);
  if (payload === null) return;
  const used = payload.used_tokens;
  const limit = payload.context_limit;
  if (typeof used !== "number" || !Number.isFinite(used) || used < 0) return;
  if (typeof limit !== "number" || !Number.isFinite(limit) || limit <= 0) {
    return;
  }
  ctx.patchTab({
    liveContextUsedTokens: used,
    liveContextLimit: limit,
  });
};

const handleToolModeChanged: FrameHandler = (_tab, frame, ctx) => {
  // V1 useChat.js:1324-1332 parity — the backend auto-detected a complex
  // intent (e.g. model-build keyword in user input) and wants the toolbar
  // to reflect it so subsequent turns in this session carry the correct
  // `tool_mode` query param.
  //
  // Backend payload shape (qai/chat/domain/stream_frame.py:197):
  //   { mode: str, previous_mode?: str | null }
  //
  // Backend auto-detect emits Python-style "model_build" (underscore), but
  // the frontend ToolModeKey uses hyphenated "model-build" — normalise
  // here. Other detected modes (translate / code / ppt / app-builder)
  // already match.
  //
  // V1 only sets when `obj.mode` is truthy; we mirror that so an
  // absent/empty mode does NOT clear a user-set manual selection.
  const payload = asPayload(frame);
  if (payload === null) return;
  const raw = payload.mode;
  if (typeof raw !== "string" || raw === "") return;
  const normalised = normaliseDetectedToolMode(raw);
  if (normalised === null) return;
  ctx.setActiveMode(normalised);
};

const handleTurnWarning: FrameHandler = (tab, frame, ctx) => {
  // V1 useChat.js:1422-1432 parity — the backend reached the configured
  // per-turn limit and emitted an advisory frame. V1 appends an inline
  // system-styled notice (`is_command_reply` bubble) to the message list
  // so the user sees the warning in-context. We mirror that by committing
  // an assistant message with `isCommandReply=true`; ChatMessageList
  // already renders such messages through the slash-command-reply path
  // (escaped plain text, no markdown — V1 parity).
  //
  // Payload field semantics (V1 useChat.js:1424):
  //   - `message` (str, optional): pre-rendered server text; when absent
  //     we compose `chat.turnLimitWarn` from `turn_count` on the client
  //     (V1 fallback).
  //
  // The active turn is still streaming when this frame arrives (V1 emits
  // it mid-loop, before any subsequent assistant chunks). We push the
  // notice into `tab.messages` directly so it does NOT collide with the
  // in-flight `streamingContent` which is reserved for the current
  // assistant turn.
  const payload = asPayload(frame);
  if (payload === null) return;
  const rawMessage = payload.message;
  const rawTurn = payload.turn_count;
  let warnText: string;
  if (typeof rawMessage === "string" && rawMessage !== "") {
    warnText = rawMessage;
  } else {
    // Compose locally via i18n. The store has no `t` instance, so we stash
    // the raw value on `meta` and let the renderer localize. Fall back to
    // a recognizable placeholder for the rare case `turn_count` is also
    // absent so the notice never renders empty.
    warnText =
      typeof rawTurn === "number" && Number.isFinite(rawTurn)
        ? `__turn_warning__:${rawTurn}`
        : `__turn_warning__:?`;
  }
  ctx.patchTab({
    messages: [
      ...tab.messages,
      {
        id: ctx.nextMessageId(),
        role: "assistant" as const,
        content: warnText,
        createdAt: Date.now(),
        isCommandReply: true,
        ...(tab.conversationId !== null
          ? { conversationId: tab.conversationId }
          : {}),
        meta: { kind: "turn_warning" },
      },
    ],
  });
};

// ---------------------------------------------------------------------------
// Sub-agent blocks — unified into the single `messages` list (user req).
// ---------------------------------------------------------------------------

/** Resolve the streaming assistant message that holds this turn's sub-agent
 *  blocks, returning `{ msgIdx, blocks }`. Opens a new streaming message (and
 *  records `activeSubAgentMessageId`) if none is open yet — so sub-agent blocks
 *  live in `messages` exactly like per-round tool cards do. Returns the patch
 *  fragment to set the new id when one was created.
 *
 *  When `roundIndex` is provided (backend-stamped `round_index`), this
 *  routes to the round's DEDICATED sub-agent message via
 *  `tab.roundSubAgentMessageIds[ri]` — an INDEPENDENT track from the main-
 *  agent's `roundMessageIds[ri]`. This gives sub-agent blocks their own
 *  message alongside (not on top of) the main-agent's per-round message
 *  that carries the round's `content` + `toolCalls` (incl. the ``agent``
 *  dispatch tool card). Two invariants coexist:
 *
 *  1. Multi-round Crit-block-overwrite fix (2026-06-29): sub-agents
 *     dispatched in different rounds of the SAME parent turn (A in
 *     round 0, B in round 1) land on DIFFERENT messages via
 *     `roundSubAgentMessageIds[0]` vs `roundSubAgentMessageIds[1]`, so
 *     B's `index=0` de-dup never clobbers A. Parallel sub-agents SPAWNED
 *     IN THE SAME ROUND (e.g. index 0 + index 1) still share one
 *     message via the SAME `roundSubAgentMessageIds[ri]`, preserving
 *     the dedup-by-index semantics WITHIN a round (a duplicate replay
 *     `subagent_start` at the same index overwrites the existing block).
 *
 *  2. Visual ordering (2026-07-02, this fix): sub-agent card renders
 *     AFTER the ``agent`` tool card because the sub-agent message is
 *     appended to `tab.messages` AFTER the parent-round message (whose
 *     `toolCalls` array holds the ``agent`` card). The natural array
 *     order IS the visual order — no template-side field-order heuristic
 *     is needed, and no interleave scenario can invert the sequence.
 *
 *  Legacy fallback (`roundIndex === null`, old backend / non-agentic): keep
 *  the single-buffer `activeSubAgentMessageId` behaviour byte-for-byte so
 *  existing specs without `round_index` stamping still pass (legacy
 *  captures collapse all sub-agents of a turn onto ONE message; the
 *  `mainAgentSummaryHeader` separator inside that message stays
 *  meaningful — hence the template still emits it). */
function ensureSubAgentMessage(
  tab: ChatTab,
  ctx: FrameHandlerContext,
  roundIndex: number | null = null,
): { msgIdx: number; messages: ChatMessage[]; activePatch: Partial<ChatTab> } {
  // Round-routed path (V2 agentic loop): use the INDEPENDENT
  // `roundSubAgentMessageIds[ri]` map. This is a SEPARATE track from
  // `roundMessageIds` (which is main-agent-only). Two rounds each spawning
  // sub-agents produce two entries in this map — never colliding with the
  // main-agent's per-round message ids.
  if (roundIndex !== null) {
    const subAgentMsgId = tab.roundSubAgentMessageIds[roundIndex];
    if (subAgentMsgId !== undefined) {
      const idx = tab.messages.findIndex((m) => m.id === subAgentMsgId);
      if (idx >= 0) {
        // Re-use this round's sub-agent message + ensure
        // `activeSubAgentMessageId` points at it so subsequent
        // `subagent_output` / `subagent_done` / `subagent_tool` frames
        // (which look up via `activeSubAgentMessageId` rather than
        // `round_index`) land on the same target. Emit NO patch fragment
        // when the id is already correct (idempotent).
        const patch: Partial<ChatTab> =
          tab.activeSubAgentMessageId === subAgentMsgId
            ? {}
            : { activeSubAgentMessageId: subAgentMsgId };
        return { msgIdx: idx, messages: [...tab.messages], activePatch: patch };
      }
    }
    // Round was given but no sub-agent message yet — fall through to open a
    // fresh one below (do NOT reuse `activeSubAgentMessageId`, which may be
    // pinned to a DIFFERENT round's sub-agent message).
  } else {
    // Legacy fallback (`roundIndex === null`, old backend / non-agentic):
    // re-use the active sub-agent message if any so consecutive
    // `subagent_start` frames in the SAME turn share a single message —
    // byte-for-byte the pre-fix behaviour. This branch is unchanged.
    const activeId = tab.activeSubAgentMessageId;
    if (activeId !== null) {
      const idx = tab.messages.findIndex((m) => m.id === activeId);
      if (idx >= 0) {
        return { msgIdx: idx, messages: [...tab.messages], activePatch: {} };
      }
    }
  }
  const id = ctx.nextMessageId();
  const msg: ChatMessage = {
    id,
    role: "assistant",
    content: "",
    createdAt: Date.now(),
    ...(tab.conversationId !== null
      ? { conversationId: tab.conversationId }
      : {}),
    subAgentBlocks: [],
    // ``kind: "subagent_summary"`` marks this as the DEDICATED sub-agent-
    // blocks message (SUBAGENT-RELOAD-PERSIST-INDEPENDENT-MSG, 2026-07-02)
    // — the same marker the backend stamps on the persisted independent
    // message (:meth:`_build_subagent_summary_message`). Keeping the marker
    // on the live-committed message too means the reload shape matches the
    // live shape (a consistency judgement-1 improvement); consumers such as
    // ``ChatMessageList.hasFollowingMainSummary`` can identify sub-agent-
    // only carriers by ``msg.meta.kind === "subagent_summary"`` without
    // caring whether the message came from a live stream or a reload.
    meta: { streaming: true, kind: "subagent_summary" },
    // NOTE: no top-level `roundIndex` field on the message — sub-agent
    // messages do NOT participate in the main-agent's `roundMessageIds`
    // routing (they own their own `roundSubAgentMessageIds` track). Not
    // stamping `roundIndex` here also means a same-round `chunk` /
    // `tool_call` frame (main-agent) will NOT accidentally fold onto this
    // sub-agent message via `handleChunk` / `handleToolCall`'s round-open
    // branches (those look up `tab.roundMessageIds[ri]`, which is
    // untouched by this function).
  };
  const messages = [...tab.messages, msg];
  // In round-routed mode, register the new message id in
  // `roundSubAgentMessageIds[ri]` so a later `subagent_start` for the
  // SAME round (parallel sub-agent at another index) folds onto the same
  // message. In legacy mode we skip the map write for byte-parity with
  // existing specs (the single-buffer `activeSubAgentMessageId` alone
  // handles same-turn sharing there).
  const activePatch: Partial<ChatTab> = {
    activeSubAgentMessageId: id,
    ...(roundIndex !== null
      ? {
          roundSubAgentMessageIds: {
            ...tab.roundSubAgentMessageIds,
            [roundIndex]: id,
          },
        }
      : {}),
  };
  return {
    msgIdx: messages.length - 1,
    messages,
    activePatch,
  };
}

/** Replace the sub-agent message's blocks in `messages` and emit the patch. */
function patchSubAgentBlocks(
  msgIdx: number,
  messages: ChatMessage[],
  blocks: SubAgentBlock[],
  extra: Partial<ChatTab>,
  ctx: FrameHandlerContext,
): void {
  const target = messages[msgIdx];
  if (target === undefined) return;
  const nextMsg: ChatMessage = { ...target, subAgentBlocks: blocks };
  ctx.patchTab({
    messages: [
      ...messages.slice(0, msgIdx),
      nextMsg,
      ...messages.slice(msgIdx + 1),
    ],
    ...extra,
  });
}

const handleSubagentStart: FrameHandler = (tab, frame, ctx) => {
  // V1 useChat.js:1345-1359 parity — backend dispatched a sub-agent; create /
  // overwrite the block at this index. Single-track: the block lives on a
  // streaming assistant message in `messages`. Backend payload (stream_frame.py
  // subagent_start factory): { index, total, prompt_preview, round_index?,
  // subagent_id?, subagent_type?, name? }.
  const payload = asPayload(frame);
  if (payload === null) return;
  const idx = payload.index;
  if (typeof idx !== "number" || !Number.isFinite(idx) || idx < 0) return;
  const totalRaw = payload.total;
  const total =
    typeof totalRaw === "number" && Number.isFinite(totalRaw) && totalRaw >= 1
      ? totalRaw
      : 1;
  const previewRaw = payload.prompt_preview;
  const preview = typeof previewRaw === "string" ? previewRaw : "";
  // Resumable / openable handle — the backend now puts it on the START frame
  // (not only on done), so the RUNNING block shows the "open in new tab" /
  // "stop" affordances immediately. Absent when persistence is unwired.
  const subagentIdRaw = payload.subagent_id;
  const subagentId =
    typeof subagentIdRaw === "string" && subagentIdRaw !== ""
      ? subagentIdRaw
      : undefined;
  // V2 UX §3.1 tail-appended: resolved profile name → i18n type-badge on the
  // running card. Absent on legacy frames / no-profile spawns → badge hidden.
  const subagentTypeRaw = payload.subagent_type;
  const subagentType =
    typeof subagentTypeRaw === "string" && subagentTypeRaw !== ""
      ? subagentTypeRaw
      : undefined;
  // V2 UX §3.1 tail-appended: LLM-supplied human-readable task label →
  // card title. Absent → card falls back to `SubAgent N` (no regression).
  const nameRaw = payload.name;
  const nameField =
    typeof nameRaw === "string" && nameRaw !== "" ? nameRaw : undefined;
  // Round-routed sub-agent message (Crit-block-overwrite fix): when the
  // backend stamps `round_index` on the frame, pin the sub-agent blocks to
  // THAT round's message. Without this, a turn that spawned sub-agents in
  // round 0, then (after a `question` interjection or just a multi-round
  // agentic loop) spawned more sub-agents in a later round, would share the
  // single `activeSubAgentMessageId` buffer — and the de-dup-by-index filter
  // in this handler (line below) would REPLACE round-0's blocks with the
  // later round's blocks (the reported "新创建的两个子 Agent 卡片把第一次创建
  // 的那个冲掉了" bug). Legacy frames without `round_index` (old backend /
  // non-agentic turns) fall back to the single-buffer behaviour via
  // `ensureSubAgentMessage(tab, ctx, null)`.
  const ri = readRoundIndex(payload);
  const { msgIdx, messages, activePatch } = ensureSubAgentMessage(tab, ctx, ri);
  const existing = messages[msgIdx]?.subAgentBlocks ?? [];
  // Replace the entry at this index if any (idempotent on duplicate frames);
  // otherwise append + sort by index.
  const filtered = existing.filter((b) => b.index !== idx);
  const block: SubAgentBlock = {
    index: idx,
    total,
    prompt_preview: preview,
    turns: [],
    rounds: 0,
    status: "running",
    _collapsed: true,
    ...(subagentId !== undefined ? { subagent_id: subagentId } : {}),
    ...(subagentType !== undefined ? { subagent_type: subagentType } : {}),
    ...(nameField !== undefined ? { name: nameField } : {}),
  };
  const next = [...filtered, block].sort((a, b) => a.index - b.index);
  patchSubAgentBlocks(msgIdx, messages, next, activePatch, ctx);

  // SubAgentRail index cache — surface the newly-spawned sub-agent on the
  // rail IMMEDIATELY (grey/running chip), without waiting for a main-tab
  // switch or the user opening the sub-agent's tab. The rail's ChatView-side
  // `railEntries` computed merges this index with any open sub-agent tab's
  // live status; here we only need to seed the entry so the chip renders.
  //
  // Field derivation (payload has no `root_conversation_id` / tree fields —
  // the frontend derives them from the PARENT tab this frame is being
  // applied to):
  //   - rootConversationId = parent's `conversationId`. For a main-agent
  //     parent this IS the root conversation. For a sub-agent parent (the
  //     grand-spawn case) `_openSubAgentTabInner` already set
  //     `conversationId: detail.root_conversation_id`, so it's still root.
  //   - parentSubagentId = the sub-agent id of the parent tab if parent is
  //     itself a sub-agent (grand-spawn); else null.
  //   - depth = parent's depth + 1 (main parent → depth 1, depth-1 parent →
  //     depth 2, ...). Legacy parents without a persisted `depth` default
  //     to 1 (same fallback the rest of the codebase uses).
  //
  // Skipped when the frame carries no `subagent_id` (legacy backend before
  // the id was stamped on the START frame — rail was a no-op anyway back
  // then; a later `openSubAgentTab` / `_fetchSubAgentIndex` fills the gap).
  // Also skipped when the parent tab has no `conversationId` (unbound blank
  // tab — no rooting possible; the rail would drop the chip on filter).
  if (subagentId !== undefined && (tab.conversationId ?? "") !== "") {
    const rootConvId = tab.conversationId as string;
    const isSubagentParent = tab.kind === "subagent";
    const parentSubagentId = isSubagentParent
      ? tab.subagentMeta?.subagentId ?? null
      : null;
    const parentDepth = isSubagentParent
      ? tab.subagentMeta?.depth ?? 1
      : 0;
    const chipTitle =
      typeof nameField === "string" && nameField !== ""
        ? nameField
        : preview !== ""
          ? preview
          : subagentId;
    ctx.upsertSubAgentIndexEntry({
      subagentId,
      rootConversationId: rootConvId,
      parentSubagentId,
      depth: parentDepth + 1,
      title: chipTitle,
      // "running" — the sub-agent just started; if it's already opened
      // as a tab, `railEntries` computed will override with the tab's
      // live status. Otherwise the chip renders greyed (`isOpen: false`)
      // and shows the running dot until a later status update.
      status: "running",
      // Owner defaults to "agent" (autonomous sub-agent, not user-taken-
      // over). A later `openSubAgentTab` / `_refreshSubAgentTab` upserts
      // the authoritative owner from the detail GET.
      owner: "agent",
    });
  }
};

/** Get (creating + ordering if needed) the ordered turn for `roundIndex`
 *  inside a sub-agent block. Returns a NEW `turns` array (immutable update)
 *  plus the target turn so callers can mutate a fresh copy. A `null`
 *  roundIndex (unstamped/legacy frame) folds into the latest turn, or a fresh
 *  round-0 turn when none exists yet — so the block still renders without
 *  crashing. This is the per-round grouping that makes the inline block show
 *  "text → tools → text → tools" in real order (main-agent parity). */
function withSubAgentTurn(
  block: SubAgentBlock,
  roundIndex: number | null,
): { turns: SubAgentTurn[]; turn: SubAgentTurn } {
  const turns = block.turns.map((tu) => ({ ...tu, tools: [...tu.tools] }));
  let ri = roundIndex;
  if (ri === null) {
    if (turns.length > 0) {
      const last = turns[turns.length - 1]!;
      return { turns, turn: last };
    }
    ri = 0;
  }
  let turn = turns.find((tu) => tu.roundIndex === ri);
  if (turn === undefined) {
    turn = { roundIndex: ri, content: "", tools: [] };
    turns.push(turn);
    turns.sort((a, b) => a.roundIndex - b.roundIndex);
  }
  return { turns, turn };
}

const handleSubagentOutput: FrameHandler = (tab, frame, ctx) => {
  // Append the text chunk to the turn for ITS round (backend-stamped
  // `round_index`), so narration interleaves with that round's tool cards in
  // real order — instead of piling all text at the end (main-agent parity).
  const payload = asPayload(frame);
  if (payload === null) return;
  const idx = payload.index;
  if (typeof idx !== "number") return;
  const text = payload.content;
  if (typeof text !== "string") return;
  const ri = readRoundIndex(payload);
  const activeId = tab.activeSubAgentMessageId;
  if (activeId === null) return;
  const msgIdx = tab.messages.findIndex((m) => m.id === activeId);
  if (msgIdx < 0) return;
  const blocks = tab.messages[msgIdx]?.subAgentBlocks ?? [];
  let mutated = false;
  const next = blocks.map((b) => {
    if (b.index !== idx) return b;
    mutated = true;
    const { turns, turn } = withSubAgentTurn(b, ri);
    turn.content += text;
    return { ...b, turns };
  });
  if (mutated) patchSubAgentBlocks(msgIdx, [...tab.messages], next, {}, ctx);
};

const handleSubagentTool: FrameHandler = (tab, frame, ctx) => {
  // Append a tool row to the turn for ITS round (backend-stamped
  // `round_index`). Field is `tool_args` (V1 wire), NOT `arguments`. V2
  // enhancement (appended per §3.1): capture the optional `tool_call_id`
  // (result-pairing key) and seed the row `status` as "running" so it renders
  // a ToolExecPanel running card until the matching `subagent_tool_result`
  // frame fills its result. Also capture the optional `emitted_at_ms`
  // wall-clock into `ts` so the sub-agent tool card's `ToolExecPanel` gets
  // the unmount-survival elapsed anchor (browser-tab switch / scroll-out
  // remount stops resetting the timer to 00:00; parity with main agent's
  // `ChatToolCall.ts`). Absent when the upstream daemon was minted before the
  // stamping change — the UI then falls back to a remount-local anchor.
  const payload = asPayload(frame);
  if (payload === null) return;
  const idx = payload.index;
  if (typeof idx !== "number") return;
  const tname = payload.tool_name;
  if (typeof tname !== "string" || tname === "") return;
  const rawArgs = payload.tool_args;
  const args: Record<string, unknown> =
    rawArgs !== null && typeof rawArgs === "object"
      ? (rawArgs as Record<string, unknown>)
      : {};
  const toolCallId =
    typeof payload.tool_call_id === "string" && payload.tool_call_id !== ""
      ? payload.tool_call_id
      : undefined;
  const emittedAt =
    typeof payload.emitted_at_ms === "number" &&
    Number.isFinite(payload.emitted_at_ms)
      ? payload.emitted_at_ms
      : undefined;
  const ri = readRoundIndex(payload);
  const activeId = tab.activeSubAgentMessageId;
  if (activeId === null) return;
  const msgIdx = tab.messages.findIndex((m) => m.id === activeId);
  if (msgIdx < 0) return;
  const blocks = tab.messages[msgIdx]?.subAgentBlocks ?? [];
  let mutated = false;
  const next = blocks.map((b) => {
    if (b.index !== idx) return b;
    mutated = true;
    const { turns, turn } = withSubAgentTurn(b, ri);
    const row: SubAgentToolCall = {
      name: tname,
      args,
      status: "running",
      ...(toolCallId !== undefined ? { tool_call_id: toolCallId } : {}),
      ...(emittedAt !== undefined ? { ts: emittedAt } : {}),
    };
    turn.tools.push(row);
    return { ...b, turns };
  });
  if (mutated) patchSubAgentBlocks(msgIdx, [...tab.messages], next, {}, ctx);
};

const handleSubagentToolResult: FrameHandler = (tab, frame, ctx) => {
  // V2 enhancement (appended per §3.1) — fill the result onto the matching
  // sub-agent tool row so it renders a settled ToolExecPanel card (output +
  // size/truncation badges). Backend payload (subagent_tool_result frame):
  //   { index, tool_name, result, ok, tool_call_id?, size?, truncated? }
  // Pairing within the block's `tools`:
  //   1. by `tool_call_id` (when present) onto a row that has no result yet;
  //   2. else by `tool_name`, newest-first, onto the most recent same-named
  //      row that has no result yet.
  const payload = asPayload(frame);
  if (payload === null) return;
  const idx = payload.index;
  if (typeof idx !== "number") return;
  const tname = payload.tool_name;
  if (typeof tname !== "string" || tname === "") return;
  const result = stringifyToolResult(payload.result);
  const ok = payload.ok !== false; // default true unless explicitly false
  const toolCallId =
    typeof payload.tool_call_id === "string" && payload.tool_call_id !== ""
      ? payload.tool_call_id
      : undefined;
  const outputSize =
    typeof payload.size === "number" ? payload.size : undefined;
  const truncated =
    typeof payload.truncated === "boolean" ? payload.truncated : undefined;
  // Per-tool wall-clock (V2 appended) — drives the same execution-time badge a
  // main-agent tool card shows, so the sub-agent card is pixel-identical.
  const durationMs =
    typeof payload.duration_ms === "number" ? payload.duration_ms : undefined;
  const activeId = tab.activeSubAgentMessageId;
  if (activeId === null) return;
  const msgIdx = tab.messages.findIndex((m) => m.id === activeId);
  if (msgIdx < 0) return;
  const blocks = tab.messages[msgIdx]?.subAgentBlocks ?? [];
  let mutated = false;
  const next = blocks.map((b) => {
    if (b.index !== idx) return b;
    // Copy turns (and their tool lists) for an immutable update, then find the
    // matching row ACROSS all turns: prefer tool_call_id (no result yet), else
    // most-recent same-named row without a result.
    const turns = b.turns.map((tu) => ({ ...tu, tools: [...tu.tools] }));
    const flat: { turn: number; row: number }[] = [];
    turns.forEach((tu, ti) =>
      tu.tools.forEach((_row, ri2) => flat.push({ turn: ti, row: ri2 })),
    );
    let hit = -1;
    if (toolCallId !== undefined) {
      hit = flat.findIndex(({ turn, row }) => {
        const r = turns[turn]!.tools[row]!;
        return r.tool_call_id === toolCallId && r.result === undefined;
      });
    }
    if (hit < 0) {
      for (let i = flat.length - 1; i >= 0; i--) {
        const { turn, row } = flat[i]!;
        const r = turns[turn]!.tools[row]!;
        if (r.name === tname && r.result === undefined) {
          hit = i;
          break;
        }
      }
    }
    if (hit < 0) return b;
    const { turn, row } = flat[hit]!;
    const r = turns[turn]!.tools[row]!;
    turns[turn]!.tools[row] = {
      ...r,
      result,
      ok,
      ...(outputSize !== undefined ? { outputSize } : {}),
      ...(truncated !== undefined ? { truncated } : {}),
      ...(durationMs !== undefined ? { duration_ms: durationMs } : {}),
      status: ok ? "done" : "error",
    };
    mutated = true;
    return { ...b, turns };
  });
  if (mutated) patchSubAgentBlocks(msgIdx, [...tab.messages], next, {}, ctx);
};

const handleSubagentDone: FrameHandler = (tab, frame, ctx) => {
  // V1 useChat.js:1382-1390 parity — mark block done + record round count.
  const payload = asPayload(frame);
  if (payload === null) return;
  const idx = payload.index;
  if (typeof idx !== "number") return;
  const roundsRaw = payload.rounds;
  const rounds =
    typeof roundsRaw === "number" && Number.isFinite(roundsRaw) && roundsRaw >= 0
      ? roundsRaw
      : 0;
  // Resumable / openable handle (backend appends it to the subagent_done frame
  // once the sub-agent session is persisted). Carrying it onto the block lets
  // SubAgentBlock show the "open in new tab" action + lets the main agent wake
  // it. Absent when persistence is unwired → block stays without an id (no
  // open affordance), backward-compatible.
  const subagentIdRaw = payload.subagent_id;
  const subagentId =
    typeof subagentIdRaw === "string" && subagentIdRaw !== ""
      ? subagentIdRaw
      : undefined;
  const activeId = tab.activeSubAgentMessageId;
  if (activeId === null) return;
  const msgIdx = tab.messages.findIndex((m) => m.id === activeId);
  if (msgIdx < 0) return;
  const blocks = tab.messages[msgIdx]?.subAgentBlocks ?? [];
  let mutated = false;
  const next = blocks.map((b) => {
    if (b.index !== idx) return b;
    mutated = true;
    return {
      ...b,
      status: "done" as const,
      rounds,
      ...(subagentId !== undefined ? { subagent_id: subagentId } : {}),
    };
  });
  if (mutated) patchSubAgentBlocks(msgIdx, [...tab.messages], next, {}, ctx);
  // SubAgentRail index cache — mark the rail chip's status as "done" so a
  // greyed (closed-tab) chip visually reflects the terminal state without
  // waiting for a snapshot re-fetch. No-op when the payload carries no
  // `subagent_id` (legacy backend without persistence — the chip stays at
  // its last-known status; harmless).
  if (subagentId !== undefined) {
    ctx.updateSubAgentIndexStatus(subagentId, "done");
  }
};

const handleSubagentError: FrameHandler = (tab, frame, ctx) => {
  // V1 useChat.js:1392-1400 parity — mark block error + record message. Field
  // is `message` (V1 wire), NOT `error`.
  const payload = asPayload(frame);
  if (payload === null) return;
  const idx = payload.index;
  if (typeof idx !== "number") return;
  const rawMessage = payload.message;
  const message =
    typeof rawMessage === "string" && rawMessage !== ""
      ? rawMessage
      : "unknown error";
  // Payload MAY carry `subagent_id` (V2 §3.1 tail-appended, same as
  // `subagent_done` — used to refresh the rail's chip status). Absent on
  // legacy backends → rail update is skipped, chip stays at its last-known
  // status. Silent no-op is fine; harmless.
  const errSubagentIdRaw = payload.subagent_id;
  const errSubagentId =
    typeof errSubagentIdRaw === "string" && errSubagentIdRaw !== ""
      ? errSubagentIdRaw
      : undefined;
  const activeId = tab.activeSubAgentMessageId;
  if (activeId === null) return;
  const msgIdx = tab.messages.findIndex((m) => m.id === activeId);
  if (msgIdx < 0) return;
  const blocks = tab.messages[msgIdx]?.subAgentBlocks ?? [];
  let mutated = false;
  const next = blocks.map((b) => {
    if (b.index !== idx) return b;
    mutated = true;
    return { ...b, status: "error" as const, error: message };
  });
  if (mutated) patchSubAgentBlocks(msgIdx, [...tab.messages], next, {}, ctx);
  // Rail chip status — same pattern as subagent_done; skipped when payload
  // has no `subagent_id`.
  if (errSubagentId !== undefined) {
    ctx.updateSubAgentIndexStatus(errSubagentId, "error");
  }
};

/** Resolve a participant's theme-aware palette colour token for a
 *  `speaker_changed` frame (block-5). Looks the participant up in the tab's
 *  discussion registry to use its assigned `config.color` index (or its order),
 *  so the colour matches what the DiscussionPanel shows; falls back to a stable
 *  hash of the id for participants not in the registry. ALWAYS returns a CSS
 *  variable reference via `discussionColorToken` — never a hardcoded colour. */
export function resolveSpeakerColor(tab: ChatTab, senderId: string): string {
  const participants = tab.discussion.participants;
  const idx = participants.findIndex((p) => p.id === senderId);
  if (idx >= 0) {
    const cfgColor = participants[idx]?.config.color;
    return discussionColorToken(
      typeof cfgColor === "number" ? cfgColor : idx,
    );
  }
  // Unknown participant — derive a stable index from the id characters.
  let hash = 0;
  for (let i = 0; i < senderId.length; i++) {
    hash = (hash * 31 + senderId.charCodeAt(i)) >>> 0;
  }
  return discussionColorToken(hash);
}

/** Resolve a discussion speaker's DISPLAY NAME from the participant roster.
 *  The live stream gets the name from the `speaker_changed.display_name` frame;
 *  a reloaded history bubble has only `senderId`, so it resolves the name here
 *  against `tab.discussion.participants`. Returns `null` when the id is not in
 *  the roster (e.g. a participant later removed) so the caller can fall back to
 *  the raw id / model name rather than show a stale/empty label. */
export function resolveSpeakerName(
  tab: ChatTab,
  senderId: string,
): string | null {
  const p = tab.discussion.participants.find((x) => x.id === senderId);
  return p ? p.display_name : null;
}

const handleSpeakerChanged: FrameHandler = (tab, frame, ctx) => {
  // Multi-Agent discussion (block-5, design §7) — the floor passed to a new
  // named participant. SOFT RESET reusing the proven `handleAgentSummary`
  // pattern (clear the in-flight `streamingContent` buffer so the next speaker's
  // text starts cleanly), EXTENDED for multi-speaker attribution:
  //   1. Commit the PRIOR speaker's trailing text (already flushed into
  //      `streamingContent` by `applyFrame` before this non-chunk frame) as a
  //      settled assistant message tagged with the prior speaker's identity, so
  //      it is NOT swallowed by the buffer clear (handleAgentSummary just drops
  //      the buffer because the main agent's summary is a CONTINUATION; here the
  //      prior speaker's words are a DISTINCT bubble that must persist).
  //   2. Reset per-round routing (`roundMessageIds` / `roundSubAgentMessageIds` /
  //      `activeToolMessageId` / `activeSubAgentMessageId`) so the NEW speaker's
  //      frames open FRESH messages instead of appending onto the prior speaker's
  //      round message (the backend re-stamps round_index per speaker turn).
  //   3. Record the new speaker on the tab (`streamingSenderId/Name/Color`) so
  //      the live streaming bubble + subsequently-opened round messages render
  //      with this participant's avatar / name / color (`senderFields`).
  //
  // ── Design note (2026-07-14) ─────────────────────────────────────────────
  // This handler is the ONLY place OUTSIDE the state-machine transitions
  // (`setStreaming` / `confirmDone` / `confirmAbort` / `recordError` /
  // `clearMessages`) that INTENTIONALLY wipes round routing (`roundMessageIds`
  // etc.) mid-stream. It is CORRECT because the backend `_stamp_round`
  // (`src/qai/chat/application/use_cases/streaming.py:302`) resets to 0 for
  // each speaker turn — the next chunk's `round_index=0` from the new speaker
  // would otherwise mis-route into the PRIOR speaker's `roundMessageIds[0]`
  // message.
  //
  // Do NOT confuse this with the `setStreaming`-vs-`resumeStreaming` split
  // introduced for the "切走切回断段" bug: that split is about WS
  // RE-SUBSCRIBE preserving live routing across a purely transport-level
  // event (the same speaker's same round continuing). `speaker_changed` is
  // a genuine SEMANTIC boundary (a different participant's floor) — routing
  // MUST be cleared here.
  // ──────────────────────────────────────────────────────────────────────
  const payload = asPayload(frame);
  if (payload === null) return;
  const sid = payload.sender_id;
  if (typeof sid !== "string" || sid === "") return;
  const displayName =
    typeof payload.display_name === "string" && payload.display_name !== ""
      ? payload.display_name
      : sid;
  const color = resolveSpeakerColor(tab, sid);
  // Capture this speaker's effective model id (back-end resolves the
  // participant's own ``model_id`` first, falling back to the tab's selected
  // model). Drives the "· model-name" suffix shown next to the speaker name
  // in the bubble meta + the per-message ``modelId`` stamped at commit time
  // (V2 enhancement 2026-06-21).
  const rawModelId = payload.model_id;
  const modelId =
    typeof rawModelId === "string" && rawModelId !== "" ? rawModelId : null;

  const patch: Partial<ChatTab> = {
    streamingContent: "",
    roundMessageIds: {},
    roundSubAgentMessageIds: {},
    activeToolMessageId: null,
    activeSubAgentMessageId: null,
    streamingSenderId: sid,
    streamingSenderName: displayName,
    streamingSenderColor: color,
    streamingSenderModelId: modelId,
  };

  // Commit the prior speaker's trailing text as its own bubble (if any),
  // attributed to the PRIOR speaker (tab.streamingSenderId BEFORE this frame).
  const priorText = tab.streamingContent;
  if (priorText !== "") {
    const priorMsg: ChatMessage = {
      id: ctx.nextMessageId(),
      role: "assistant",
      content: priorText,
      createdAt: Date.now(),
      ...(tab.conversationId !== null
        ? { conversationId: tab.conversationId }
        : {}),
      ...(tab.streamingSenderId !== null
        ? {
            senderId: tab.streamingSenderId,
            ...(tab.streamingSenderName !== null
              ? { senderName: tab.streamingSenderName }
              : {}),
            ...(tab.streamingSenderColor !== null
              ? { senderColor: tab.streamingSenderColor }
              : {}),
            // Carry the PRIOR speaker's model id forward (V2 enhancement
            // 2026-06-21): the bubble shows "name · model" so a discussion
            // transcript records which model each role spoke with.
            ...(tab.streamingSenderModelId !== null
              ? { modelId: tab.streamingSenderModelId }
              : {}),
          }
        : {}),
      meta: { streaming: true },
    };
    patch.messages = [...tab.messages, priorMsg];
  }
  ctx.patchTab(patch);
};

const handleAgentSummary: FrameHandler = (_tab, _frame, ctx) => {
  // V1 useChat.js:1402-1408 parity — main agent is about to produce its
  // summary; reset the in-flight assistant text buffer so the summary
  // appears cleanly *after* the sub-agent blocks instead of being
  // prefixed by the sub-agent text. Sub-agent blocks remain attached as
  // the separator before the summary (rendered by SubAgentBlock template +
  // i18n key `index.mainAgentSummaryHeader`).
  ctx.patchTab({ streamingContent: "" });
};

/** Handle a `reasoning` ("thinking") frame.
 *
 *  Reasoning tokens stream alongside / before the visible answer. We bind them
 *  to the SAME per-round assistant message the answer CHUNK uses (round_index
 *  zero-inference path, mirroring `handleChunk`), accumulating into the
 *  message's `reasoning` field so the UI renders a collapsible thinking block
 *  ABOVE that round's answer text. The backend stamps REASONING frames with
 *  their round (`_ROUND_STAMPED_FRAME_TYPES`), so the round path is the normal
 *  case. When no round_index is stamped (older backend / non-agentic single
 *  turn) we open (or reuse) a standalone streaming assistant message and
 *  accumulate onto its `reasoning` — no tab-level buffer, so no confirmDone
 *  change is needed.
 *
 *  Producers: cloud reasoning models' `delta.reasoning_content` (previously
 *  discarded) and the internal query-service adapter's noise-filtered thinking.
 */
const handleReasoning: FrameHandler = (tab, frame, ctx) => {
  const payload = asPayload(frame);
  if (payload === null || typeof payload.text !== "string") return;
  const text = payload.text;
  if (text === "") return;

  const ri = readRoundIndex(payload);
  if (ri !== null) {
    let openId = tab.roundMessageIds[ri];
    let messages = tab.messages;
    if (openId === undefined) {
      // Reasoning arrived before any answer chunk / tool_call for this round:
      // open the round's assistant message now (same shape as handleChunk).
      const id = ctx.nextMessageId();
      const reqId = readRequestId(payload);
      const roundMsg: ChatMessage = {
        id,
        role: "assistant",
        content: "",
        reasoning: "",
        createdAt: Date.now(),
        ...(tab.conversationId !== null
          ? { conversationId: tab.conversationId }
          : {}),
        ...senderFields(tab, payload),
        ...roundModelFields(tab),
        meta: {
          streaming: true,
          roundIndex: ri,
          ...(reqId !== "" ? { request_id: reqId } : {}),
        },
      };
      messages = [...tab.messages, roundMsg];
      ctx.patchTab({
        messages,
        roundMessageIds: { ...tab.roundMessageIds, [ri]: id },
      });
      openId = id;
    }
    const next = messages.map((m) =>
      m.id === openId
        ? { ...m, reasoning: (m.reasoning ?? "") + text }
        : m,
    );
    ctx.patchTab({ messages: next });
    return;
  }

  // No round_index: reuse the last streaming assistant message if one exists,
  // else open a standalone streaming assistant message to host the thinking
  // block. Accumulate onto its `reasoning` field (no tab-level buffer).
  const last = tab.messages[tab.messages.length - 1];
  if (
    last !== undefined &&
    last.role === "assistant" &&
    last.meta?.streaming === true
  ) {
    const next = tab.messages.map((m, i) =>
      i === tab.messages.length - 1
        ? { ...m, reasoning: (m.reasoning ?? "") + text }
        : m,
    );
    ctx.patchTab({ messages: next });
    return;
  }
  const id = ctx.nextMessageId();
  const reqId = readRequestId(payload);
  const msg: ChatMessage = {
    id,
    role: "assistant",
    content: "",
    reasoning: text,
    createdAt: Date.now(),
    ...(tab.conversationId !== null
      ? { conversationId: tab.conversationId }
      : {}),
    ...senderFields(tab, payload),
    ...roundModelFields(tab),
    meta: {
      streaming: true,
      ...(reqId !== "" ? { request_id: reqId } : {}),
    },
  };
  ctx.patchTab({ messages: [...tab.messages, msg] });
};

/**
 * DISC-1 implementation orchestration — observability frame handlers (§22.9).
 *
 * These four handlers translate the OFF-by-default backend implementation
 * orchestration's structured control-plane frames into per-tab
 * `implementation` state (`TabImplementationState`), which the
 * ImplementationPanel renders. An idle tab carries `DEFAULT_IMPLEMENTATION_
 * STATE` (phase `"none"`) — ordinary chat / discussion is therefore
 * untouched until a run starts.
 *
 * Frame contract (mirrors `tests/.../chatTabs.implementation.spec.ts`):
 *   - `plan_ready`                  → seed items + phase "implementing" + runId
 *   - `implementation_item_started` → currentItem + that item status in_progress
 *   - `implementation_item_finished`→ item status/resultSummary/lastError + clear currentItem
 *   - `implementation_phase_changed`→ phase / currentItem (terminal phase omits → cleared)
 *
 * Tolerance: payload field missing / wrong type ⇒ skip-frame or apply sensible
 * defaults (never throw). Junk items in `plan_ready` are dropped. Item frames
 * that arrive without a matching `plan_ready` (lost first frame / out-of-order
 * feed) still append a row so the panel stays consistent.
 */

/** Read a nullable string off a payload field — `null` when the value is not
 *  a non-empty string. (No shared helper exported; kept local to the impl
 *  handler block for clarity.) */
function readNullableStrField(
  payload: Record<string, unknown>,
  key: string,
): string | null {
  const v = payload[key];
  return typeof v === "string" && v !== "" ? v : null;
}

/** Default values for the detail VM fields the LIVE control-plane frames do
 *  NOT carry (description / acceptanceCriteria / verifyCommand / dependsOn /
 *  attemptCount). These are populated by the GET (`wireToItem` in
 *  `stores/implementation.ts`); a row created from a frame alone fills them
 *  with neutral empties so the VM shape stays complete (the next `reload()`
 *  rehydrates the real values). */
const EMPTY_ITEM_DETAILS = {
  description: "",
  acceptanceCriteria: [] as string[],
  verifyCommand: "",
  dependsOn: [] as string[],
  attemptCount: 0,
};

/** Map one snake_case `plan_ready` item summary to a camelCase VM, tolerant of
 *  missing fields (an item with no usable `id` is dropped by the caller). */
function itemSummaryToVM(raw: unknown): ImplementationItemVM | null {
  if (raw === null || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const id = typeof r.id === "string" && r.id !== "" ? r.id : "";
  if (id === "") return null;
  return {
    id,
    title: typeof r.title === "string" ? r.title : "",
    status:
      typeof r.status === "string" && r.status !== "" ? r.status : "pending",
    assignedRole: readNullableStrField(r, "assigned_role"),
    suggestedRole: readNullableStrField(r, "suggested_role"),
    resultSummary: null,
    lastError: null,
    ...EMPTY_ITEM_DETAILS,
  };
}

/** Handle `plan_ready` — a run is starting; seed items + phase + runId. */
const handlePlanReady: FrameHandler = (tab, frame, ctx) => {
  const payload = asPayload(frame);
  if (payload === null) return;
  const runId =
    typeof payload.run_id === "string" && payload.run_id !== ""
      ? payload.run_id
      : null;
  // No run_id ⇒ ignore (state stays idle).
  if (runId === null) return;
  const rawItems = Array.isArray(payload.items) ? payload.items : [];
  const items: ImplementationItemVM[] = [];
  for (const raw of rawItems) {
    const vm = itemSummaryToVM(raw);
    if (vm !== null) items.push(vm);
  }
  ctx.patchTab({
    implementation: {
      ...tab.implementation,
      phase: "implementing",
      runId,
      currentItem: null,
      items,
    },
  });
};

/** Handle `implementation_item_started` — set currentItem + mark that item
 *  in_progress; append a new row when the item is unknown (feed consistency). */
const handleImplementationItemStarted: FrameHandler = (tab, frame, ctx) => {
  const payload = asPayload(frame);
  if (payload === null) return;
  const itemId =
    typeof payload.item_id === "string" && payload.item_id !== ""
      ? payload.item_id
      : "";
  if (itemId === "") return; // no item_id ⇒ ignore
  const title = typeof payload.title === "string" ? payload.title : null;
  const assignedRole = readNullableStrField(payload, "assigned_role");

  const existing = tab.implementation.items;
  const hasIt = existing.some((it) => it.id === itemId);
  let items: ImplementationItemVM[];
  if (hasIt) {
    items = existing.map((it) =>
      it.id === itemId
        ? {
            ...it,
            status: "in_progress",
            ...(title !== null ? { title } : {}),
            ...(assignedRole !== null ? { assignedRole } : {}),
          }
        : it,
    );
  } else {
    items = [
      ...existing,
      {
        id: itemId,
        title: title ?? "",
        status: "in_progress",
        assignedRole,
        suggestedRole: null,
        resultSummary: null,
        lastError: null,
        ...EMPTY_ITEM_DETAILS,
      },
    ];
  }
  ctx.patchTab({
    implementation: { ...tab.implementation, currentItem: itemId, items },
  });
};

/** Handle `implementation_item_finished` — record status/resultSummary/lastError
 *  on that item, clear currentItem if it was this one; append a row when the
 *  item is unknown (feed consistency). */
const handleImplementationItemFinished: FrameHandler = (tab, frame, ctx) => {
  const payload = asPayload(frame);
  if (payload === null) return;
  const itemId =
    typeof payload.item_id === "string" && payload.item_id !== ""
      ? payload.item_id
      : "";
  if (itemId === "") return;
  const status =
    typeof payload.status === "string" && payload.status !== ""
      ? payload.status
      : "done";
  const resultSummary = readNullableStrField(payload, "result_summary");
  const lastError = readNullableStrField(payload, "last_error");

  const existing = tab.implementation.items;
  const hasIt = existing.some((it) => it.id === itemId);
  let items: ImplementationItemVM[];
  if (hasIt) {
    items = existing.map((it) =>
      it.id === itemId
        ? { ...it, status, resultSummary, lastError }
        : it,
    );
  } else {
    items = [
      ...existing,
      {
        id: itemId,
        title: "",
        status,
        assignedRole: null,
        suggestedRole: null,
        resultSummary,
        lastError,
        ...EMPTY_ITEM_DETAILS,
      },
    ];
  }
  const currentItem =
    tab.implementation.currentItem === itemId
      ? null
      : tab.implementation.currentItem;
  ctx.patchTab({
    implementation: { ...tab.implementation, currentItem, items },
  });
};

/** Handle `implementation_phase_changed` — update phase + currentItem. A
 *  terminal phase that omits `current_item` clears it; an interim phase
 *  (e.g. `paused`) may carry `current_item` to highlight which item parked. */
const handleImplementationPhaseChanged: FrameHandler = (tab, frame, ctx) => {
  const payload = asPayload(frame);
  if (payload === null) return;
  const phase =
    typeof payload.phase === "string" && payload.phase !== ""
      ? payload.phase
      : "";
  if (phase === "") return; // no phase ⇒ ignore
  const currentItem = readNullableStrField(payload, "current_item");
  ctx.patchTab({
    implementation: {
      ...tab.implementation,
      phase,
      currentItem,
    },
  });
};

/**
 * Handle an `injected_message` frame (V2 enhancement — mid-turn user
 * injection). The backend has folded the user's "inject" button content into
 * the SAME run at the inter-round seam: it appended the text to the wire as a
 * `role:user` message and persisted it.
 *
 * Injection is control-plane-only (user decision 2026-06-24): the composer
 * inserts an OPTIMISTIC grey / pending `role:user` bubble straight into the
 * conversation (`meta.injected` + `meta.pending`) the instant the user clicks
 * inject — NOT into the send-queue. This handler RECONCILES that bubble:
 *   - pair the FIRST still-pending injected bubble by trimmed text, clear its
 *     `meta.pending` (and adopt the backend's persisted `message_id` +
 *     `round_index`) so it becomes a committed user message — never a second
 *     duplicate bubble; OR
 *   - if no pending bubble matches (e.g. a reload lost the optimistic local
 *     state, or the frame arrived for a tab that never showed one), commit a
 *     fresh user bubble: the backend already persisted it + showed it to the
 *     model, so the conversation truth must include it (State-Truth-First).
 */
const handleInjectedMessage: FrameHandler = (tab, frame, ctx) => {
  const payload = asPayload(frame);
  if (payload === null || typeof payload.text !== "string") return;
  const text = payload.text;
  if (text === "") return;
  const trimmed = text.trim();

  const rawId = payload.message_id;
  const messageId =
    typeof rawId === "string" && rawId !== "" ? rawId : ctx.nextMessageId();
  const ri = readRoundIndex(payload);

  // Reconcile the FIRST still-pending optimistic injection bubble (paired by
  // trimmed text) into a committed user message, rather than appending a
  // second one (anti-duplication).
  const pendingIdx = tab.messages.findIndex(
    (m) =>
      m.role === "user" &&
      (m.meta as Record<string, unknown> | undefined)?.["injected"] === true &&
      (m.meta as Record<string, unknown> | undefined)?.["pending"] === true &&
      m.content.trim() === trimmed,
  );
  if (pendingIdx >= 0) {
    const existing = tab.messages[pendingIdx]!;
    const committed: ChatMessage = {
      ...existing,
      // Adopt the backend's persisted id so a later history reload pairs.
      id: messageId,
      content: text,
      meta: {
        injected: true,
        ...(ri !== null ? { roundIndex: ri } : {}),
      },
    };
    ctx.patchTab({
      messages: [
        ...tab.messages.slice(0, pendingIdx),
        committed,
        ...tab.messages.slice(pendingIdx + 1),
      ],
    });
    return;
  }

  // No pending bubble to reconcile → commit a fresh one (reload / orphan path).
  const userMsg: ChatMessage = {
    id: messageId,
    role: "user",
    content: text,
    createdAt: Date.now(),
    ...(tab.conversationId !== null
      ? { conversationId: tab.conversationId }
      : {}),
    meta: {
      injected: true,
      ...(ri !== null ? { roundIndex: ri } : {}),
    },
  };

  ctx.patchTab({
    messages: [...tab.messages, userMsg],
  });
};

/**
 * Dispatch table keyed by `frame.frame_type`. `applyFrame` looks up the
 * handler here; an unknown / `error` frame_type simply has no entry and
 * is ignored (the transport handles `error` via `recordError`).
 */
export const FRAME_HANDLERS: Readonly<Record<string, FrameHandler>> = {
  chunk: handleChunk,
  reasoning: handleReasoning,
  tool_call: handleToolCall,
  tool_result: handleToolResult,
  end: handleEnd,
  tool_mode_changed: handleToolModeChanged,
  turn_warning: handleTurnWarning,
  compaction_progress: handleCompactionProgress,
  context_usage: handleContextUsage,
  subagent_start: handleSubagentStart,
  subagent_output: handleSubagentOutput,
  subagent_tool: handleSubagentTool,
  subagent_tool_result: handleSubagentToolResult,
  subagent_done: handleSubagentDone,
  subagent_error: handleSubagentError,
  agent_summary: handleAgentSummary,
  speaker_changed: handleSpeakerChanged,
  plan_ready: handlePlanReady,
  implementation_item_started: handleImplementationItemStarted,
  implementation_item_finished: handleImplementationItemFinished,
  implementation_phase_changed: handleImplementationPhaseChanged,
  injected_message: handleInjectedMessage,
};
