// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Multi-tab Chat store (PR-054).
 *
 * Implements the per-tab 4-state machine + multi-tab isolation
 * invariants per refactor-plan §10.5 / §10.6 and api-contract §4.
 *
 * The store keeps three classes of state:
 *
 *   1. Reactive Pinia state (`tabs`, `activeTabId`) — what the UI
 *      consumes. `tabs` is a plain array (NOT a Map) because Pinia /
 *      Vue 3 reactivity over `Map` is awkward and the array order is
 *      itself a UI contract (tab strip ordering).
 *
 *   2. Per-tab transient handles (`AbortController`, `WebSocket`).
 *      These are NOT reactive Pinia state — they live in private
 *      module-level WeakRef-style maps keyed by tab id. This sidesteps
 *      both (a) Vue reactivity wrapping native objects whose
 *      identity must be stable, and (b) the historical Pinia bug
 *      around `WebSocket` constructors. Each tab's controller is
 *      strictly isolated; cancelling one is invisible to the others
 *      (refactor-plan §10.6 invariant 2).
 *
 *   3. Per-tab message buffers — kept in `tab.messages`. Streaming
 *      assembly accumulates into `tab.streamingContent`; on `done` the
 *      assistant message is committed and `streamingContent` cleared.
 *
 * 4-state machine (§10.5):
 *
 *      idle ── send ──→ streaming ── done frame ──→ idle
 *                       │
 *                       ├── stop / cancel ──→ aborting ── confirm ──→ idle
 *                       └── error frame    ──→ error    ── reset ──→ idle
 *
 * Transition contract:
 *   - `idle → streaming`        : `setStreaming(id)` (called by transport on `send`)
 *   - `streaming → idle`        : `confirmDone(id)` (called on server `done`)
 *   - `streaming → aborting`    : `requestCancel(id)` (called on user click)
 *   - `aborting → idle`         : `confirmAbort(id)` (called when transport closes)
 *   - `streaming → error`       : `recordError(id, err)` (called on `error` frame)
 *   - `error → idle`            : `resetError(id)` (called on user reset)
 *
 * Front-end NEVER short-circuits `streaming → idle` without backend
 * confirmation (invariant 4).
 */
import { defineStore } from "pinia";
import type { ApiError } from "@/api";
import type { ChatStreamFrame } from "@/types/streaming";
// Internal aliases for the types/consts now living in _chatTabsTypes.ts.
// External code keeps importing from "@/stores/chatTabs" via the
// `export *` re-export below.
import type {
  TabId,
  ConversationId,
  ChatMessageRole,
  ToolModeKey,
  ToolParams,
  ModelParams,
  QueuedMessage,
  ChatMessagePerf,
  ChatMessageUsage,
  ChatMessage,
  ChatToolCall,
  NetworkRetryState,
  ChatTab,
  ChatTabStatus,
  ChatTabsState,
  DiscussionConfig,
  SubAgentBlock,
  SubAgentIndexEntry,
  TabImplementationState,
  AwayAutoAnswerSettings,
} from "./_chatTabsTypes";
import {
  DEFAULT_MODEL_PARAMS,
  DEFAULT_TOOL_PARAMS,
  DEFAULT_DISCUSSION_CONFIG,
  DEFAULT_IMPLEMENTATION_STATE,
  DEFAULT_AWAY_AUTO_ANSWER,
  AWAY_AUTO_ANSWER_MIN_SECONDS,
  AWAY_AUTO_ANSWER_MAX_SECONDS,
  HISTORY_PAGE_SIZE,
  MAX_QUEUE_SIZE,
  MAX_OPEN_TABS,
} from "./_chatTabsTypes";
import type { PersistedTabsLayout, PersistedTabEntry } from "./chatTabs/chatTabsPersistence";
import {
  loadSessionToolOverride,
  saveSessionToolOverride,
} from "./chatTabs/chatTabsPersistence";
// SSE per-frame_type handlers (F3 cohesion split). `applyFrame` is now a
// thin guard + dispatch over this table; each frame kind's reducer logic
// lives in `chatTabs/frameHandlers.ts`.
import {
  FRAME_HANDLERS,
  resolveSpeakerColor,
  resolveSpeakerName,
} from "./chatTabs/frameHandlers";
import { extractLatestTodoList } from "./chatTabs/harnessToolFrames";
// Per-tab transient handles (AbortController / WebSocket maps) — ARCH-1
// cohesion split. Re-exported below so existing `@/stores/chatTabs` import
// paths (transport / appBuilder / tests) keep working unchanged.
import {
  _resetChatTabsTransient,
  clearAbortController,
  clearWebSocket,
  attachSubAgentStream,
  clearSubAgentStream,
  markSubAgentTabLive,
  unmarkSubAgentTabLive,
  bufferStreamingChunk,
  takeBufferedStreamingChunk,
  hasBufferedStreamingChunk,
  scheduleStreamingFlush,
  cancelStreamingFlush,
  clearStreamingBuffer,
  bufferRoundChunk,
  takeBufferedRoundChunk,
  hasBufferedRoundChunk,
  peekRoundChunkTarget,
  scheduleRoundChunkFlush,
  cancelRoundChunkFlush,
  bufferToolOutputDelta,
  takeDirtyToolOutput,
  dirtyToolOutputKeys,
  clearToolOutputBuffer,
  scheduleToolOutputFlush,
  cancelToolOutputFlush,
  getSubAgentLastAppliedSeq,
  recordSubAgentAppliedSeq,
  clearSubAgentLastSeq,
} from "./chatTabs/transientHandles";
// Monotonic client-side id generators (ARCH-1 cohesion split).
import {
  nextLocalTabId,
  nextMessageId,
  nextQueueId,
} from "./chatTabs/idGenerators";
// History-page fetching + row → ChatMessage mapping (ARCH-1 cohesion split).
import { fetchNewestPage, fetchOlderPage } from "./chatTabs/historyLoader";
// Persisted history row → ChatMessage mapper. Statically imported (it is
// already pulled into this chunk via `historyLoader` → `historyMapper`), so a
// dynamic `import()` here yields no separate chunk (Rollup warns "dynamic
// import will not move module into another chunk"). One static import instead.
import { mapHistoryItems } from "./chatTabs/historyMapper";
import { forgetChatScroll } from "@/composables/chat/useChatScrollMemory";
// App-level per-tab transport manager (singleton). Used ONLY as a read-only
// truth-source in `refreshFromSnapshot` to tell whether a sub-agent tab's
// current `streaming` status is driven by a LIVE take-over turn on the main
// transport (`isInFlight()` — the SAME discriminator ChatView's Stop path
// uses, ChatView.vue:516-520). `useChatTransports()` is a singleton that only
// touches the store lazily inside its own functions, so this top-level import
// introduces no module-eval cycle. `peekTransport` never creates a transport.
import { useChatTransports } from "@/composables/chat/useChatTransports";
// Control-channel (WS fast path for answer / cancel_tool). Statically imported
// here because it is ALREADY statically pulled into this chunk by
// useChatTransport / useComposerSubmit / ChatView, so a dynamic `import()`
// gains no separate chunk (Rollup "dynamic import will not move module into
// another chunk" warning). It has no import back to this store, so no cycle.
import { useChatControlChannel } from "@/composables/chat/useChatControlChannel";
// Turn-commit builders (done / abort → committed ChatMessage) — ARCH-1.
import {
  buildConfirmDoneMessages,
  buildConfirmAbortMessages,
} from "./chatTabs/messageCommit";

// ---------------------------------------------------------------------------
// Types & constants live in `_chatTabsTypes.ts` (cohesion split).
// Re-export them so existing `import { ... } from "@/stores/chatTabs"`
// paths keep working without churn.
// ---------------------------------------------------------------------------

export * from "./_chatTabsTypes";

// ---------------------------------------------------------------------------
// Per-tab transient handles (AbortController / WebSocket) now live in
// `chatTabs/transientHandles.ts`. Re-export the public surface so external
// call sites importing from `@/stores/chatTabs` are unaffected.
// ---------------------------------------------------------------------------

export {
  _resetChatTabsTransient,
  getOrCreateAbortController,
  resetAbortController,
  peekAbortController,
  clearAbortController,
  attachWebSocket,
  peekWebSocket,
  clearWebSocket,
  markSubAgentTabLive,
  unmarkSubAgentTabLive,
  isSubAgentTabLive,
  getSubAgentLastAppliedSeq,
  recordSubAgentAppliedSeq,
  clearSubAgentLastSeq,
} from "./chatTabs/transientHandles";

// (Tool-result helpers `stringifyToolResult` / `isToolErrorOutput` /
//  `normaliseDetectedToolMode` live in `./chatTabs/frameHandlers`; the id
//  generators in `./chatTabs/idGenerators`; the history mapper in
//  `./chatTabs/historyMapper`.)

/**
 * Normalise ONE sub-agent live event into the equivalent MAIN-AGENT
 * `ChatStreamFrame`, so an independent sub-agent tab can drive the SAME
 * `applyFrame` → `FRAME_HANDLERS` pipeline the main agent uses (one render
 * path, identical tool cards — no bespoke liveText accumulation).
 *
 *   subagent_output       → chunk        {text}
 *   subagent_tool         → tool_call    {tool_name, arguments, tool_call_id}
 *   subagent_tool_result  → tool_result  {tool_name, result, tool_call_id,
 *                                          size, truncated}
 *
 * `subagent_start` / `subagent_done` / `subagent_error` carry no per-turn
 * render payload of their own (start/terminal are handled by the streaming
 * status + the authoritative snapshot refresh on close) → return `null`.
 * Returns `null` for anything unrecognised so the caller skips it.
 */
function subAgentEventToChatFrame(
  ev: Record<string, unknown>,
  seq: number,
): ChatStreamFrame | null {
  const type = typeof ev.type === "string" ? ev.type : "";
  const tcid =
    typeof ev.tool_call_id === "string" && ev.tool_call_id !== ""
      ? ev.tool_call_id
      : undefined;
  // The sub-agent kernel stamps every output / tool / tool_result event with
  // its agentic-loop `round` (backend `agent_tool.py`: `"round": ev.round_no`,
  // 1-based). The frontend's `frameHandlers` route a frame to its round STRICTLY
  // by `payload.round_index` (>= 0); without it every sub-agent frame fell back
  // to the legacy single-buffer / lead-in-heuristic path, so a sub-agent's round
  // 2+ content was mis-grouped and never surfaced live (only a close+reopen,
  // which rebuilds from the round-indexed persisted snapshot, showed it all).
  // Forward `round` as `round_index` (passed through verbatim — same as the main
  // agent's `_stamp_round(frame, round_no)`), so the live view groups by round
  // identically to the reopened snapshot. Absent / invalid → omitted (legacy
  // fallback unchanged for old emitters).
  const roundRaw = ev.round;
  const roundIndex =
    typeof roundRaw === "number" && Number.isFinite(roundRaw) && roundRaw >= 0
      ? roundRaw
      : undefined;
  switch (type) {
    case "subagent_output":
      return {
        frame_id: `sa-c-${seq}`,
        frame_type: "chunk",
        sequence: seq,
        payload: {
          text: typeof ev.content === "string" ? ev.content : "",
          ...(roundIndex !== undefined ? { round_index: roundIndex } : {}),
        },
      };
    case "subagent_reasoning":
      // Sub-agent model "thinking" tokens forwarded from the kernel's
      // REASONING passthrough (父子统一 with takeover: the takeover path
      // renders a `reasoning` frame). Map to the SAME `reasoning` frame the
      // main agent uses so `handleReasoning` routes it into the collapsible
      // thinking block — identical live rendering to takeover.
      return {
        frame_id: `sa-r-${seq}`,
        frame_type: "reasoning",
        sequence: seq,
        payload: {
          text: typeof ev.content === "string" ? ev.content : "",
          ...(roundIndex !== undefined ? { round_index: roundIndex } : {}),
        },
      };
    case "subagent_tool_partial":
      // Live PARTIAL tool output (sub-agent exec stdout/stderr increments, or
      // a cloud `generating_args` arg-streaming increment). Map to a
      // `tool_result` frame with `partial: true` + `delta` so `handleToolResult`
      // streams it onto the running tool card — identical to the takeover
      // path's partial `tool_result` frames.
      return {
        frame_id: `sa-tp-${seq}`,
        frame_type: "tool_result",
        sequence: seq,
        payload: {
          tool_name: typeof ev.tool_name === "string" ? ev.tool_name : "tool",
          result: typeof ev.delta === "string" ? ev.delta : "",
          delta: typeof ev.delta === "string" ? ev.delta : "",
          partial: true,
          ...(tcid !== undefined ? { tool_call_id: tcid } : {}),
          ...(roundIndex !== undefined ? { round_index: roundIndex } : {}),
        },
      };
    case "subagent_tool":
      return {
        frame_id: `sa-tc-${seq}`,
        frame_type: "tool_call",
        sequence: seq,
        payload: {
          tool_name: typeof ev.tool_name === "string" ? ev.tool_name : "tool",
          arguments:
            ev.tool_args && typeof ev.tool_args === "object"
              ? (ev.tool_args as Record<string, unknown>)
              : {},
          ...(tcid !== undefined ? { tool_call_id: tcid } : {}),
          ...(roundIndex !== undefined ? { round_index: roundIndex } : {}),
        },
      };
    case "subagent_tool_result":
      return {
        frame_id: `sa-tr-${seq}`,
        frame_type: "tool_result",
        sequence: seq,
        payload: {
          tool_name: typeof ev.tool_name === "string" ? ev.tool_name : "tool",
          result: typeof ev.result === "string" ? ev.result : "",
          ...(tcid !== undefined ? { tool_call_id: tcid } : {}),
          ...(typeof ev.size === "number" ? { size: ev.size } : {}),
          ...(typeof ev.truncated === "boolean"
            ? { truncated: ev.truncated }
            : {}),
          ...(typeof ev.duration_ms === "number"
            ? { duration_ms: ev.duration_ms }
            : {}),
          ...(ev.cancelled === true ? { cancelled: true } : {}),
          ...(roundIndex !== undefined ? { round_index: roundIndex } : {}),
        },
      };
    default:
      // subagent_start / subagent_done / subagent_error — no render frame.
      return null;
  }
}


// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export interface OpenTabInput {
  readonly id?: TabId;
  readonly title?: string;
  readonly conversationId?: ConversationId;
  readonly modelId?: string;
  /** V1 parity — see `ChatTab.modelProvider`. Empty string by default. */
  readonly modelProvider?: string;
  /** Tab kind (V2 enhancement; appended). `"subagent"` opens a sub-agent
   *  inspection/takeover tab — see `ChatTab.kind`. Defaults to `"chat"`. */
  readonly kind?: "chat" | "subagent";
  /** Sub-agent binding for a `kind === "subagent"` tab — see
   *  `ChatTab.subagentMeta`. */
  readonly subagentMeta?: {
    readonly subagentId: string;
    /** ROOT (top-of-tree main-agent) conversation id — see
     *  `ChatTab.subagentMeta.rootConversationId`. */
    readonly rootConversationId: string;
    /** Direct-parent sub-agent id (`null`/absent = depth-1). */
    readonly parentSubagentId?: string | null;
    /** Recursion depth (1 = first-level, 2 = grand, ...). */
    readonly depth?: number;
    readonly status: string;
    readonly owner: string;
    /** Sub-agent's own context usage — see `ChatTab.subagentMeta`. */
    readonly usedTokens?: number;
    readonly budgetTokens?: number;
    readonly ratio?: number;
    /** REAL (un-clamped) occupancy — see `ChatTab.subagentMeta`. */
    readonly rawUsedTokens?: number;
    readonly rawRatio?: number;
    /** Sub-agent's OWN model — see `ChatTab.subagentMeta`. */
    readonly modelId?: string;
    readonly modelProvider?: string;
  };
}

/**
 * Duck-typed guard: does this thrown error represent a GENUINE
 * "conversation does not exist in the backend" (HTTP 404 /
 * `chat.conversation_not_found`)? Used to self-heal a stale `conversationId`
 * that came from persisted localStorage but is absent from a fresh/empty DB
 * (AGENTS.md §🔴 State-Truth-First). Deliberately field-duck-typed (not an
 * `instanceof ApiError`) to keep the store decoupled from the API barrel
 * (same rationale as the lazy `import("@/api")` below). MUST stay strict:
 * a transient network / 5xx error is NOT a not-found and must not drop a
 * valid id.
 */
function isConversationNotFound(err: unknown): boolean {
  if (err === null || typeof err !== "object") {
    return false;
  }
  const e = err as { code?: unknown; status?: unknown };
  return e.code === "chat.conversation_not_found" || e.status === 404;
}

/**
 * Non-degradation guard for the terminal sub-agent snapshot refresh
 * (`_refreshSubAgentTab`). Returns true when adopting `next` (the freshly
 * fetched backend snapshot) would LOSE content that is already rendered in
 * `current` (the live-accumulated messages) — i.e. the persisted snapshot is
 * momentarily behind the live stream we just rendered.
 *
 * State-Truth-First 铁律 1: the snapshot is normally authoritative, but at the
 * exact moment `subagent_done` fires the DB may not yet contain the final
 * summary text / last round's tool result. Overwriting then makes the tool
 * card + summary visibly vanish (the reported bug). We treat the snapshot as
 * "poorer" — and skip the overwrite this turn — when it has:
 *   - fewer messages, OR
 *   - fewer total tool calls (a tool card would disappear), OR
 *   - lost the trailing assistant summary text (current has non-empty trailing
 *     assistant content but the snapshot's is empty).
 * A later snapshot (status poll / re-open) reconciles once the DB catches up,
 * so this never permanently pins stale content.
 */
export function _snapshotWouldLoseContent(
  current: readonly ChatMessage[],
  next: readonly ChatMessage[],
): boolean {
  // Nothing rendered yet → never "poorer"; always adopt the snapshot.
  if (current.length === 0) {
    return false;
  }
  if (next.length < current.length) {
    return true;
  }
  const countToolCalls = (msgs: readonly ChatMessage[]): number =>
    msgs.reduce((n, m) => n + (m.toolCalls?.length ?? 0), 0);
  if (countToolCalls(next) < countToolCalls(current)) {
    return true;
  }
  const lastAssistantText = (msgs: readonly ChatMessage[]): string => {
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i]!.role === "assistant") {
        return (msgs[i]!.content ?? "").trim();
      }
    }
    return "";
  };
  // Lost the final summary: current produced trailing assistant text but the
  // snapshot's trailing assistant message is empty.
  if (lastAssistantText(current) !== "" && lastAssistantText(next) === "") {
    return true;
  }
  // Lost usage coverage: the live stream accumulated per-round usage on
  // multiple assistant messages (giving an accurate cumulative ↑↓ count),
  // but the persisted snapshot collapsed them into fewer messages — or the
  // snapshot's usage-bearing assistant count is lower. Adopting the snapshot
  // would replace the richer live usage data with a poorer one, causing the
  // ↑ token line to flash a correct value then drop to a smaller number
  // (the "↑5 → ↑1" regression). Keep the live messages when the snapshot
  // has fewer usage-bearing assistant turns than what is already rendered.
  const countUsageMessages = (msgs: readonly ChatMessage[]): number =>
    msgs.reduce(
      (n, m) => n + (m.role === "assistant" && m.usage != null ? 1 : 0),
      0,
    );
  if (countUsageMessages(next) < countUsageMessages(current)) {
    return true;
  }
  return false;
}


/**
 * Module-level guard against concurrent `openSubAgentTab` calls for the same
 * sub-agent id.  The async method awaits a network fetch + dynamic import
 * before creating the tab — without this guard, rapid clicks queue multiple
 * in-flight opens that all pass the "existing tab" check (tab not yet created)
 * and ultimately create duplicate tabs.
 */
const _openingSubAgentIds = new Set<string>();

/**
 * Module-level in-flight tracker for `interruptSubAgent`. When the user
 * rapidly clicks ⏹ on the same sub-agent multiple times (or two UI surfaces
 * for the same sub-agent both fire an interrupt simultaneously), the second
 * call MUST share the first call's outcome — not silently dedupe to `true`
 * and then have the first call's `aborted=false` rollback flip the UI back
 * to `streaming`, leaving the second click visibly "succeeded" but the
 * sub-agent actually still running. Sharing the same promise guarantees
 * every concurrent caller observes the SAME final state (committed or
 * rolled-back), so the UI never lies about what happened.
 *
 * The map is keyed by subagentId and the value is the in-flight Promise
 * returned by `interruptSubAgent`. Entries are deleted in `finally` so a
 * later interrupt (e.g. on a new run with the same id after a wake)
 * starts a fresh request.
 */
const _inflightInterrupts = new Map<string, Promise<boolean>>();

/**
 * Module-level guard for SUBA-MODEL-RACE-1: while a `setSubAgentModel`
 * PATCH is in flight for a sub-agent, mark its id here so concurrent
 * `_refreshSubAgentTab` calls (typically from `refreshFromSnapshot` at
 * terminal time) know to SKIP overwriting `modelId` with the GET's
 * possibly-stale value. The PATCH itself will write the authoritative
 * new model when it completes, so no data is lost.
 *
 * Cleared in `setSubAgentModel`'s `finally`. Module-scope (not instance
 * state) is fine because sub-agent ids are globally unique ULIDs.
 */
const _inflightModelPatches = new Set<string>();

/**
 * Persistence key for a SUB-agent tab's session tool / SKILL override.
 *
 * Sub-agent tabs carry the PARENT conversation's id, so keying their override
 * by conversationId would collide with the parent's main-agent override. We
 * therefore key sub-agent overrides by their own subagentId under a distinct
 * `sub:` prefix (both ids are ULIDs, but the prefix keeps the two namespaces
 * cleanly separated and prevents the destroy-purge-by-conversationId path from
 * touching a sub-agent's saved override).
 */
function _subOverrideKey(subagentId: string): string {
  return `sub:${subagentId}`;
}

/**
 * (β) Flat tab-strip persistence: the layout emits every tab (main-agent or
 * sub-agent at any depth) as its own top-level entry, in the SAME order as
 * `state.tabs`. So the active-index resolve is a straight `findIndex` over the
 * layout — no dedup-projection needed.
 *
 * Resolution rules:
 *   1. Active is a normal main `kind:"chat"` tab → find its position in the
 *      layout via `conversationId`.
 *   2. Active is a `kind:"subagent"` tab → find its position by `subagentId`.
 *   3. Anything else (active id stale, layout mismatch) → fall back to 0 so
 *      the reload lands on the first tab instead of past-end.
 */
function computePersistedActiveIndex(
  activeTabId: TabId | null,
  stateTabs: readonly ChatTab[],
  layoutTabs: readonly PersistedTabEntry[],
): number {
  if (activeTabId === null) return 0;
  const activeTab = stateTabs.find((t) => t.id === activeTabId);
  if (activeTab === undefined) return 0;

  const idx = layoutTabs.findIndex((e) => {
    if (activeTab.kind === "subagent") {
      return (
        e.kind === "subagent" &&
        e.subagentId === activeTab.subagentMeta?.subagentId
      );
    }
    return (
      e.kind !== "subagent" && e.conversationId === activeTab.conversationId
    );
  });
  return idx >= 0 ? idx : 0;
}

export const useChatTabsStore = defineStore("chatTabs", {
  state: (): ChatTabsState => ({
    tabs: [],
    activeTabId: null,
    // Per-root-conversation cache of ALL sub-agent sessions (open OR closed
    // in `state.tabs`). Filled by `_fetchSubAgentIndex(convId)` on main-tab
    // history load; drives the SubAgentRail's chip list so closed sub-agents
    // remain reachable (chip greyed, one click re-hydrates). See
    // `SubAgentIndexEntry` docstring for the full lifecycle.
    subAgentIndex: {},
  }),

  getters: {
    activeTab(state): ChatTab | null {
      const id = state.activeTabId;
      if (id === null) {
        return null;
      }
      return state.tabs.find((t) => t.id === id) ?? null;
    },
    tabById(state): (id: TabId) => ChatTab | null {
      return (id: TabId) => state.tabs.find((t) => t.id === id) ?? null;
    },
    /**
     * Whether the active tab is "busy" — generating OR mid-abort.
     *
     * V1 parity: V1 drives EVERY stop affordance from a single boolean
     * `isStreaming` (useChat.js:121) via `isStreamingHere` (useChat.js:314),
     * so its floating stop button and composer stop button can never disagree.
     * V2 has a finer `streaming → aborting → idle` state machine; if each stop
     * affordance maps that to "show stop" with a DIFFERENT condition (one
     * checks only `streaming`, the other `streaming || aborting`), they fall
     * out of sync during the `aborting` window (floating button vanishes while
     * the composer button stays in stop state). This single getter is the one
     * shared truth both affordances must read, restoring V1's "one source
     * drives all stop UI" behaviour.
     */
    isActiveTabBusy(): boolean {
      const tab = this.activeTab;
      return tab !== null && (tab.status === "streaming" || tab.status === "aborting");
    },

    /**
     * Whether the soft cap on simultaneously-open tabs has been reached.
     * The tab strip's "+" button and the "New conversation" affordance read
     * this to disable themselves (and surface a hint) once `MAX_OPEN_TABS`
     * tabs are open. Restoring an EXISTING conversation is not gated by this
     * (see `openTab`'s cap guard, which only blocks brand-new blank tabs).
     *
     * Only MAIN-agent tabs (`kind !== "subagent"`) count toward the cap.
     * Sub-agent tabs are drill-in views spawned by a main session and must
     * not consume a user-facing "session" slot, so they are excluded here.
     */
    atTabLimit(state): boolean {
      return state.tabs.filter((t) => t.kind !== "subagent").length >= MAX_OPEN_TABS;
    },

    /**
     * The "effective active MAIN tab id" — resolves the parent main-agent tab
     * even when the raw `activeTabId` points at a sub-agent tab (the user has
     * drilled into a sub-agent's transcript).
     *
     * Two cases:
     *   1. Active is a main-agent tab (`kind !== "subagent"`) → returns its id.
     *   2. Active is a sub-agent tab → walks `subagentMeta.rootConversationId`
     *      to find the main-agent tab bound to that conversation and returns
     *      its id (falls back to the raw active id when the main tab is not
     *      open — defence in depth).
     *
     * Consumers:
     *   - `ChatTabStrip` uses it for the `.chat-tab-strip__tab--active`
     *     highlight so the top strip still lights up the parent main tab while
     *     the user reads a sub-agent's transcript.
     *   - `ChatView` uses it (indirectly, via `activeMainTab` computed) to
     *     compute the SubAgentRail's data source (all sub-agents rooted at
     *     the active main tab's conversation).
     *
     * This getter replaces the participant-heavy `effectiveActiveTabId`
     * projection the pre-β code carried in ChatTabStrip.vue: same semantics,
     * hoisted to the store so both consumers share ONE derivation (judge 1:
     * single source of truth).
     */
    activeMainTabId(state): TabId | null {
      const id = state.activeTabId;
      if (id === null) return null;
      const active = state.tabs.find((t) => t.id === id);
      if (active === undefined) return id;
      if (active.kind !== "subagent") return id;
      const rootConvId = active.subagentMeta?.rootConversationId ?? "";
      if (rootConvId === "") return id;
      const parent = state.tabs.find(
        (t) => t.kind !== "subagent" && t.conversationId === rootConvId,
      );
      return parent !== undefined ? parent.id : id;
    },

    /**
     * Lightweight, JSON-serialisable snapshot of the tab *layout* for
     * persistence across reloads (see `chatTabs/chatTabsPersistence.ts`).
     * Only the skeleton (conversation binding + title + order + which is
     * active) — never messages or runtime handles.
     */
    persistedLayout(state): PersistedTabsLayout {
      // (β) Flat tab-strip persistence: every tab in `state.tabs` — main-agent
      // or sub-agent (at any depth) — emits its own top-level layout entry,
      // in the same order. A sub-agent entry stores its `subagentId` and
      // forces `conversationId: null` (the authoritative key is the id; the
      // sub-agent's root_conversation_id is re-fetched from the detail GET
      // on restore). Per-session tool overrides are keyed independently by
      // conversationId / `_subOverrideKey(subagentId)`, so they aren't
      // duplicated here.
      const tabs = state.tabs.flatMap((t) => {
        if (t.kind === "subagent") {
          const sid = t.subagentMeta?.subagentId;
          // Defensive: a sub-agent tab without subagentId is unrestorable —
          // emit a normal-looking entry so it falls back to the chat restore
          // path (blank tab). `_openSubAgentTabInner` always sets subagentId,
          // so this branch is belt-and-braces.
          if (sid === undefined || sid === "") {
            return [{ conversationId: null, title: t.title }];
          }
          return [
            {
              conversationId: null,
              title: t.title,
              kind: "subagent" as const,
              subagentId: sid,
            },
          ];
        }
        const o = t.sessionToolOverride;
        // Persist the per-session tool / SKILL override (keyed implicitly by
        // the tab's conversation) so it survives close+reopen AND reload.
        // Only when non-empty, so tabs without an override serialise unchanged.
        const hasOverride =
          o !== undefined &&
          (o.disabledTools.length > 0 || o.disabledSkills.length > 0);
        const entry: PersistedTabEntry = {
          conversationId: t.conversationId,
          title: t.title,
          ...(hasOverride
            ? {
                sessionToolOverride: {
                  disabledTools: [...o!.disabledTools],
                  disabledSkills: [...o!.disabledSkills],
                },
              }
            : {}),
        };
        return [entry];
      });
      const activeIndex = computePersistedActiveIndex(
        state.activeTabId,
        state.tabs,
        tabs,
      );
      return { version: 1, tabs, activeIndex };
    },

    /**
     * Sidebar-badge counts for a tab's conversation, recomputed from the
     * tab's in-memory `messages` so the "Recent Chats" rounds / tool-call
     * badges can update immediately after a turn finishes — WITHOUT a full
     * list refetch (V1 parity: `useChat.js:560-562` recomputes these in
     * `saveCurrentConversation` and writes them onto the in-memory
     * conversations entry; the sidebar renders that entry directly).
     *
     * Counting semantics are aligned 1:1 with the backend list SQL
     * (`conversation_repository.py:131-143`) so the optimistic value matches
     * what the next `GET /api/chat/conversations` would compute (no
     * refresh-time jump):
     *   - `round_count`     = COUNT of persisted `role='user'` messages.
     *     Slash-command echoes (`isCommandMsg`) are display-only and never
     *     persisted (chatTabs.ts:851 / useChat.js:578-579), so they are
     *     excluded here to match the DB row count.
     *   - `tool_call_count` = SUM over messages of `toolCalls.length`
     *     (backend: `SUM(json_array_length(tool_calls_json))`), i.e. the
     *     total number of tool invocations, NOT the number of tool messages.
     *
     * Pure getter — reads only `tab.messages`, mutates nothing.
     */
    conversationCounts(
      state,
    ): (id: TabId) => { round_count: number; tool_call_count: number } {
      return (id: TabId) => {
        const tab = state.tabs.find((t) => t.id === id);
        if (tab === undefined) {
          return { round_count: 0, tool_call_count: 0 };
        }
        let roundCount = 0;
        let toolCallCount = 0;
        for (const m of tab.messages) {
          if (m.role === "user" && m.isCommandMsg !== true) {
            roundCount += 1;
          }
          if (Array.isArray(m.toolCalls)) {
            toolCallCount += m.toolCalls.length;
          }
        }
        return { round_count: roundCount, tool_call_count: toolCallCount };
      };
    },
  },

  actions: {
    // ---------------------------------------------------------------
    // CRUD
    // ---------------------------------------------------------------

    openTab(input: OpenTabInput = {}): ChatTab {
      // V1 parity (useModels.js:13 — `selectedModelId` is a single global
      // ref shared across all sessions; a new session reuses whatever model
      // the user last picked, persisted via /api/preferences). V2 stores the
      // selection per-tab, so a brand-new chat (no explicit model, not
      // restoring an existing conversation) inherits the currently-active
      // tab's selection instead of snapping back to the "qai-default"
      // placeholder — which ChatComposer's auto-select watcher would then
      // force to the first *cloud* model, silently dropping a local-model
      // selection (audit Bug #3). Restoring a conversation (conversationId
      // provided) keeps loading its model from history, and an explicit
      // modelId always wins.
      const prev = this.activeTab;
      // Soft cap (MAX_OPEN_TABS): refuse to open a brand-new BLANK tab once
      // the cap is reached, so users can't fan out an unbounded number of
      // live transports. Restoring an existing conversation (conversationId
      // provided — e.g. clicking a sidebar history item) is always allowed:
      // gating it would make history un-openable once the cap is hit. UI
      // affordances (tab strip "+" / new-conversation button) disable
      // themselves via the `atTabLimit` getter, so hitting this guard is a
      // defensive fallback. Returns the current active tab (or the first tab)
      // unchanged so callers that read the return value don't crash.
      // Only MAIN-agent tabs count toward the cap; sub-agent drill-in tabs
      // (kind === "subagent") never consume a session slot.
      if (
        input.conversationId === undefined &&
        this.tabs.filter((t) => t.kind !== "subagent").length >= MAX_OPEN_TABS
      ) {
        return prev ?? this.tabs[0]!;
      }
      const inheritModel =
        input.modelId === undefined &&
        input.conversationId === undefined &&
        prev !== null &&
        prev.modelId !== "" &&
        prev.modelId !== "qai-default";
      const resolvedModelId = input.modelId ?? (inheritModel ? prev!.modelId : "qai-default");
      const resolvedModelProvider =
        input.modelProvider ?? (inheritModel ? prev!.modelProvider : "");
      const tab: ChatTab = {
        id: input.id ?? nextLocalTabId(),
        conversationId: input.conversationId ?? null,
        // Default to empty so consumers (ChatTabStrip / sidebar / export)
        // render the LOCALIZED `chat.tab.untitled` via their `title || t(...)`
        // fallback. A hardcoded "New chat" literal here leaked an
        // untranslated English title into the tab strip under zh-CN/zh-TW.
        title: input.title ?? "",
        modelId: resolvedModelId,
        modelProvider: resolvedModelProvider,
        status: "idle",
        messages: [],
        streamingContent: "",
        lastError: null,
        createdAt: Date.now(),
        lastActiveAt: Date.now(),
        activeMode: null,
        toolParams: { ...DEFAULT_TOOL_PARAMS },
        modelParams: { ...DEFAULT_MODEL_PARAMS },
        streamingToolCalls: [],
        activeToolMessageId: null,
        roundMessageIds: {},
        roundSubAgentMessageIds: {},
        activeSubAgentMessageId: null,
        streamingUsage: null,
        streamingPerf: null,
        streamingSubAgentBlocks: [],
        streamingRequestId: null,
        loadingMore: false,
        hasMoreMessages: false,
        messagesOldestPos: -1,
        messageQueue: [],
        queueExpanded: false,
        networkRetry: null,
        loadingHistory: false,
        todoList: [],
        todoExpanded: false,
        pendingQuestion: null,
        // Multi-Agent discussion (block-5): a fresh tab starts with discussion
        // OFF, so it behaves as an ordinary single-agent chat (deep-copied so
        // each tab owns its own participant registry).
        discussion: {
          ...DEFAULT_DISCUSSION_CONFIG,
          participants: [],
        },
        // DISC-1 (§22.9): a fresh tab starts with an idle implementation state
        // (phase "none", no items). Populated only when the OFF-by-default
        // backend implementation orchestration emits its control-plane frames.
        implementation: {
          ...DEFAULT_IMPLEMENTATION_STATE,
          items: [],
        },
        streamingSenderId: null,
        streamingSenderName: null,
        streamingSenderColor: null,
        streamingSenderModelId: null,
        pinnedSpeaker: null,
        ...(input.kind !== undefined ? { kind: input.kind } : {}),
        ...(input.subagentMeta !== undefined
          ? { subagentMeta: input.subagentMeta }
          : {}),
      };
      // Restore the per-session tool / SKILL override (persisted in the
      // conversationId/subagentId-keyed store, independent of the open-tabs
      // layout — see chatTabsPersistence). This makes the toggles survive
      // CLOSING a tab and REOPENING it, not just a full reload.
      //
      // Two cases:
      //  • MAIN-agent tab → key by conversationId. A brand-new unsent tab
      //    (conversationId null) has nothing to key on yet.
      //  • SUB-agent tab (`kind: "subagent"`) → key by its OWN subagentId
      //    (NEVER the parent conversationId — that would collide/串扰 with the
      //    parent's main-agent override, since a sub-agent tab carries the
      //    parent's conversationId). First open with no saved sub-agent
      //    override INHERITS the parent conversation's main-agent override
      //    (read-only; not written back) so the sub-agent defaults to the same
      //    tool set as its parent; the user can then change it and that change
      //    persists under the sub-agent's own key.
      if (input.kind === "subagent") {
        const sid = input.subagentMeta?.subagentId;
        let seed = sid ? loadSessionToolOverride(_subOverrideKey(sid)) : undefined;
        if (
          seed === undefined &&
          input.conversationId !== undefined &&
          input.conversationId !== null
        ) {
          // First open → inherit the parent conversation's main-agent override.
          seed = loadSessionToolOverride(input.conversationId);
        }
        if (seed !== undefined) {
          tab.sessionToolOverride = {
            disabledTools: [...seed.disabledTools],
            disabledSkills: [...seed.disabledSkills],
          };
        }
      } else if (
        input.conversationId !== undefined &&
        input.conversationId !== null
      ) {
        const persisted = loadSessionToolOverride(input.conversationId);
        if (persisted !== undefined) {
          tab.sessionToolOverride = {
            disabledTools: [...persisted.disabledTools],
            disabledSkills: [...persisted.disabledSkills],
          };
        }
      }
      this.tabs = [...this.tabs, tab];
      this.activeTabId = tab.id;
      return tab;
    },

    closeTab(tabId: TabId, mode: "keep" | "destroy" = "keep"): void {
      const idx = this.tabs.findIndex((t) => t.id === tabId);
      if (idx < 0) {
        return;
      }
      // Capture this tab's override persistence key BEFORE removing it —
      // needed by the `destroy` branch below to purge its persisted override
      // (sub:<id> for a sub-agent tab, conversationId for a main-agent tab).
      const closingOverrideKey = this._persistKeyForTab(tabId);
      // Capture the conversationId too so the scroll-memory cleanup in the
      // `destroy` branch can purge BOTH possible keys (`c:<convId>` and
      // `t:<tabId>`). See useChatScrollMemory `chatScrollKey()` for the
      // dual-key fallback rationale.
      const closingConversationId = this.tabs[idx]?.conversationId ?? null;
      const closingTab = this.tabs[idx];
      // SUBA-MAIN-CLOSE-1 cascade — when closing a MAIN tab (kind !== "subagent")
      // that owns sub-agent tabs (matched by
      // `subagentMeta.rootConversationId === tab.conversationId`), recursively
      // close them FIRST. β flat tab-strip model: sub-agent tabs are siblings
      // in `state.tabs`, and cascade-closing is driven by the root
      // conversation edge (single truth source; state-truth-first). Without
      // this the sub-agent tabs would become orphans (their parent
      // conversation gone from the tab strip while they linger).
      //
      // Why this is safe / non-recursive-explosion:
      //   - The cascade only matches sub-agent tabs (`kind === "subagent"`)
      //     whose root_conversation_id equals THIS main tab's conversationId.
      //     A sub-agent tab's own `closeTab` never re-enters the cascade
      //     (kind !== "subagent" gate below rejects it).
      //   - We snapshot the matching ids BEFORE recursing so concurrent
      //     mutations from inner `closeTab` calls don't skip siblings.
      //   - Grand / great-grand sub-agents (depth ≥ 2) share the SAME
      //     rootConversationId, so a single main-tab close cascades ALL
      //     descendants in one pass.
      if (
        closingTab !== undefined &&
        closingTab.kind !== "subagent" &&
        closingTab.conversationId !== null &&
        closingTab.conversationId !== ""
      ) {
        const parentConvId = closingTab.conversationId;
        const childTabIds = this.tabs
          .filter(
            (t) =>
              t.kind === "subagent" &&
              t.subagentMeta?.rootConversationId === parentConvId,
          )
          .map((t) => t.id);
        for (const childId of childTabIds) {
          this.closeTab(childId, mode);
        }
      }
      // Cancel any in-flight work for this tab — but ONLY this tab.
      // Other tabs' controllers are untouched (§10.6 inv 3).
      clearAbortController(tabId);
      clearWebSocket(tabId);
      clearStreamingBuffer(tabId);
      // Release any live sub-agent WS/SSE subscription this tab held. No-op
      // for ordinary tabs.
      clearSubAgentStream(tabId);
      // Forget every from_seq tracker this tab held. On the next open (a
      // fresh sub-agent tab), the WS subscribes with from_seq=0 (full
      // replay from the top). Prior applied-sequence state was
      // per-(tabId, subagentId) and only meaningful for THIS tab instance.
      clearSubAgentLastSeq(tabId);

      // Re-resolve `idx` AFTER the cascade above so the active-fallback
      // selection below picks a neighbour from the POST-cascade `this.tabs`.
      const idxNow = this.tabs.findIndex((t) => t.id === tabId);
      this.tabs = this.tabs.filter((t) => t.id !== tabId);
      if (this.activeTabId === tabId) {
        const fallback =
          this.tabs[idxNow] ?? this.tabs[idxNow - 1] ?? this.tabs[0];
        this.activeTabId = fallback?.id ?? null;
      }
      // Closing the LAST tab must not leave a blank, tab-less workspace.
      // Like Notepad++ (closing the last document leaves a fresh "new" one) and
      // browsers, replace it with a brand-new blank tab so there's always at
      // least one open tab to work in. `openTab` activates the new tab.
      if (this.tabs.length === 0) {
        this.openTab();
      }
      // `destroy` = the underlying conversation is gone (deleted from the
      // sidebar), not merely closed. Purge its persisted per-session tool
      // override so the keyed store doesn't leak/grow with dead conversations.
      // `keep` (an ordinary tab close) intentionally PRESERVES the override so
      // reopening the same conversation restores the user's toggles.
      if (mode === "destroy" && closingOverrideKey !== null) {
        saveSessionToolOverride(closingOverrideKey, null);
      }
      // Same `keep` vs `destroy` philosophy for the per-tab scroll position:
      // a `destroy` (the conversation is gone) purges the saved scroll so the
      // session-scoped store doesn't accumulate dead entries; a `keep`
      // (ordinary close) preserves it so reopening the SAME conversation
      // restores the reading position. The helper purges every possible key
      // (sub-agent-scoped + conversationId-scoped + tabId-scoped) — see
      // `chatScrollKey()`. A sub-agent tab keys on its own `subagentId`
      // (SCROLL LEAK FIX 2026-07-11), so pass it here so a destroyed sub-agent's
      // scroll entry is purged too.
      if (mode === "destroy") {
        forgetChatScroll(
          tabId,
          closingConversationId,
          closingTab?.subagentMeta?.subagentId ?? null,
        );
      }
      // Same `keep` vs `destroy` split for the SubAgentRail index cache
      // (`state.subAgentIndex[convId]`). A `destroy` (the underlying
      // conversation was deleted from the sidebar) means every sub-agent
      // rooted at that conversation is also gone server-side, so its cached
      // entries are dead — leaving them would grow a permanent dictionary of
      // ghost keys across the session (State-Truth-First 铁律 4: don't let
      // one truth source drift from another; the backend list is authoritative
      // and this cache mirrors it). A `keep` close intentionally PRESERVES the
      // cache so reopening the same conversation from the sidebar shows the
      // rail immediately (no fetch flash). Only meaningful when closing a
      // MAIN tab: a sub-agent tab's own `closeTab` intentionally never
      // touches the parent's cache — that's the whole point of the "chip
      // survives close" semantic.
      if (
        mode === "destroy" &&
        closingTab !== undefined &&
        closingTab.kind !== "subagent" &&
        closingConversationId !== null &&
        closingConversationId !== "" &&
        this.subAgentIndex[closingConversationId] !== undefined
      ) {
        const next = { ...this.subAgentIndex };
        delete next[closingConversationId];
        this.subAgentIndex = next;
      }
    },

    /**
     * Close every open tab in one action (the tab strip overflow menu's
     * "close all sessions" affordance). Cancels each tab's in-flight work
     * (AbortController / WebSocket / streaming buffer / sub-agent SSE) exactly
     * like {@link closeTab}, then clears the list. Mirroring `closeTab`'s
     * "never leave a blank, tab-less workspace" rule (Notepad++/browser
     * behaviour), a single fresh blank tab is opened afterwards and activated,
     * so the user always lands on a usable empty chat instead of an empty
     * workspace.
     */
    closeAllTabs(): void {
      for (const tab of this.tabs) {
        clearAbortController(tab.id);
        clearWebSocket(tab.id);
        clearStreamingBuffer(tab.id);
        clearSubAgentStream(tab.id);
        // Block 2 — drop this tab's from_seq trackers along with the WS
        // (mirrors the single-close path above).
        clearSubAgentLastSeq(tab.id);
      }
      this.tabs = [];
      this.activeTabId = null;
      this.openTab();
    },

    switchTab(tabId: TabId): void {
      const found = this.tabs.find((t) => t.id === tabId);
      if (found === undefined) {
        return;
      }
      this.activeTabId = tabId;
      this._touch(tabId);
      // Tiered-flush parity (perf): a background streaming tab coalesces its
      // chunks on a coarse timer; when it becomes the foreground tab the user
      // is now watching, flush any pending buffered text synchronously so the
      // switch lands on the up-to-date content immediately (no ~150ms tail
      // before the next coarse timer fires). Subsequent chunks then schedule on
      // the active (per-frame) tier via appendStreamingChunk/appendRoundChunk.
      // Both are no-ops when nothing is buffered.
      this.flushStreamingNow(tabId);
      this.flushRoundChunkNow(tabId);
      // SubAgentRail opportunistic backfill — a main tab that missed its
      // `loadHistoryMessages` sub-agent-index fetch (e.g. layout-restored tab
      // whose history load short-circuited on `messages.length > 0`) still
      // deserves a rail. `_fetchSubAgentIndex` is idempotent (no-ops when the
      // cache is already populated), so this is cheap on every switch.
      const rootConvId =
        found.kind === "subagent"
          ? found.subagentMeta?.rootConversationId ?? ""
          : found.conversationId ?? "";
      if (rootConvId !== "" && (this.subAgentIndex[rootConvId] ?? []).length === 0) {
        void this._fetchSubAgentIndex(rootConvId);
      }
    },

    /**
     * Reorder the open tabs by moving the `fromId` tab so it takes the slot
     * of the `toId` tab (drag-to-reorder in the tab strip). The dragged tab is
     * removed from its current position and re-inserted at the target tab's
     * index, shifting the rest — matching the familiar "drop where you let go"
     * behaviour of browser/editor tab bars. `activeTabId` is unchanged
     * (reordering never switches the active session). No-op when either id is
     * unknown or `fromId === toId`.
     */
    reorderTabs(fromId: TabId, toId: TabId): void {
      if (fromId === toId) {
        return;
      }
      const fromIdx = this.tabs.findIndex((t) => t.id === fromId);
      const toIdx = this.tabs.findIndex((t) => t.id === toId);
      if (fromIdx < 0 || toIdx < 0) {
        return;
      }
      const next = this.tabs.slice();
      const [moved] = next.splice(fromIdx, 1);
      if (moved === undefined) {
        return;
      }
      next.splice(toIdx, 0, moved);
      this.tabs = next;
    },

    /**
     * Rebuild the tab set from a persisted layout (reload/restart restore).
     * Reopens one tab per entry (binding its conversationId + title) in the
     * saved order and re-activates the saved active tab. Returns the number of
     * tabs restored (0 when the layout is empty → caller should fall back to
     * opening a default tab).
     *
     * Messages are NOT restored here — the caller lazily fetches each
     * conversation's history via `loadHistoryMessages` (backend = source of
     * truth). Only call this when `tabs` is empty (initial app load); it does
     * not merge with existing tabs.
     *
     * Sub-agent entries (`kind === "subagent"`) are restored by synchronously
     * creating a placeholder sub-agent tab (so the saved tab ORDER is
     * preserved) and then asynchronously hydrating it via
     * `_hydrateRestoredSubAgentTab` (which fetches the sub-agent detail +
     * transcript and resubscribes the live stream). If the sub-agent has been
     * deleted server-side, the placeholder tab self-closes — never blocks
     * normal tab restore.
     */
    restoreLayout(layout: PersistedTabsLayout): number {
      if (this.tabs.length > 0 || layout.tabs.length === 0) {
        return 0;
      }
      const created: ChatTab[] = [];
      for (const entry of layout.tabs) {
        if (entry.kind === "subagent" && typeof entry.subagentId === "string") {
          // Sub-agent placeholder: synchronously create a `kind: "subagent"`
          // tab so the strip shows it immediately in the saved position.
          // `rootConversationId`/`status`/`owner` are placeholders, filled
          // in by `_hydrateRestoredSubAgentTab`'s GET. `conversationId` is
          // null on the tab (it WILL be set to the real root conversation
          // id by the hydrate's `_patchTab`, mirroring `openSubAgentTab`).
          const subagentId = entry.subagentId;
          const placeholderTab = this.openTab({
            title: entry.title,
            kind: "subagent",
            subagentMeta: {
              subagentId,
              rootConversationId: "",
              status: "running",
              owner: "",
            },
          });
          created.push(placeholderTab);
          // Hydrate asynchronously — fire-and-forget. Errors (404 / deleted
          // sub-agent / network) are swallowed inside the helper, which
          // closes the placeholder tab so a stale persisted id self-heals.
          void this._hydrateRestoredSubAgentTab(placeholderTab.id, subagentId);
          continue;
        }
        // openTab with a conversationId is never gated by MAX_OPEN_TABS;
        // a null-conversation (blank) entry is a normal new tab. Pass the
        // saved title so the strip shows it immediately (before history
        // loads). `conversationId: null` → omit so it counts as a blank tab.
        const tab = this.openTab(
          entry.conversationId !== null
            ? { conversationId: entry.conversationId, title: entry.title }
            : { title: entry.title },
        );
        // Restore the per-session tool / SKILL override saved with this tab
        // (close+reopen / reload persistence). Normalised on load; only a
        // non-empty diff is present here.
        if (entry.sessionToolOverride !== undefined) {
          this._patchTab(tab.id, {
            sessionToolOverride: {
              disabledTools: [...entry.sessionToolOverride.disabledTools],
              disabledSkills: [...entry.sessionToolOverride.disabledSkills],
            },
          });
        }
        // Push the parent so `created[layout.activeIndex]` keeps pointing at
        // the right entry.
        created.push(tab);
      }
      const activeTab = created[layout.activeIndex] ?? created[0];
      if (activeTab !== undefined) {
        this.activeTabId = activeTab.id;
      }
      return created.length;
    },

    /**
     * Hydrate a sub-agent placeholder tab created by {@link restoreLayout}.
     *
     * Pulls the authoritative sub-agent detail (transcript + status + meta +
     * own model), patches the placeholder with the real `rootConversationId`
     * + messages + metadata, then resubscribes the live WS stream (so a
     * still-running sub-agent picks up its broadcast from the missed
     * frames; a terminal one simply replays its full buffer once and closes).
     *
     * Error path: when the sub-agent has been deleted server-side (404 /
     * `chat.subagent_not_found`) or the detail otherwise cannot be loaded,
     * the placeholder tab is CLOSED so a stale persisted subagentId never
     * leaves a dead, unhydratable tab on the strip across reloads (mirrors
     * how a stale main-agent `conversationId` is self-healed by
     * `isConversationNotFound` elsewhere in this store).
     */
    async _hydrateRestoredSubAgentTab(
      tabId: TabId,
      subagentId: string,
    ): Promise<void> {
      const { apiJson } = await import("@/api");
      interface SubAgentMessageItem {
        role: string;
        text?: string | null;
        tool_calls?: ChatToolCall[] | null;
        tool_call_id?: string | null;
        name?: string | null;
        created_at?: string | null;
        usage?: ChatMessageUsage | null;
        meta?: Record<string, unknown> | null;
      }
      interface SubAgentDetail {
        subagent_id: string;
        status: string;
        owner: string;
        subagent_type?: string;
        title?: string | null;
        prompt_preview?: string | null;
        rounds?: number;
        root_conversation_id: string;
        parent_subagent_id?: string | null;
        depth?: number;
        created_at?: string;
        updated_at?: string;
        messages?: SubAgentMessageItem[];
        used_tokens?: number;
        budget_tokens?: number;
        ratio?: number;
        raw_used_tokens?: number;
        raw_ratio?: number;
        model_id?: string | null;
        model_provider?: string | null;
        allow_spawn?: boolean;
      }
      let detail: SubAgentDetail;
      try {
        detail = await apiJson<SubAgentDetail>(
          "GET",
          `/api/chat/subagents/${encodeURIComponent(subagentId)}`,
        );
      } catch (err) {
        // Self-heal: a sub-agent that no longer exists (404 / deleted) leaves
        // a permanently-broken placeholder if we keep it on the strip — close
        // it so the persisted layout converges with backend truth. Network /
        // 5xx failures land here too; closing the tab is the conservative
        // choice (the user can reopen from the sidebar once the backend
        // recovers — losing one ghost tab is preferable to a stuck skeleton).
        console.warn(
          `[chatTabs] _hydrateRestoredSubAgentTab: closing stale placeholder for sub-agent ${subagentId}`,
          err,
        );
        // Only close if the placeholder is still there and is still the
        // placeholder shape (defensive: user may have manually closed it
        // already while the fetch was in flight).
        const tab = this.tabs.find((t) => t.id === tabId);
        if (tab !== undefined && tab.kind === "subagent") {
          this.closeTab(tabId);
        }
        return;
      }
      const tab = this.tabs.find((t) => t.id === tabId);
      // Placeholder may have been manually closed by the user during the
      // round-trip — bail out silently in that case.
      if (tab === undefined || tab.kind !== "subagent") {
        return;
      }
      // Map the sub-agent's persisted transcript using the SAME mapper as
      // `_openSubAgentTabInner` / `_refreshSubAgentTab` (single source of
      // truth — no divergence between the open path and the restore path).
      const baseIso = detail.created_at ?? detail.updated_at;
      const baseTs = baseIso ? Date.parse(baseIso) : Date.now();
      const createdMs = Number.isFinite(baseTs) ? baseTs : Date.now();
      const historyItems = (detail.messages ?? []).map((m, i) => ({
        id: `${subagentId}:${i}`,
        role: m.role,
        text: typeof m.text === "string" ? m.text : "",
        created_at:
          typeof m.created_at === "string" && m.created_at
            ? m.created_at
            : new Date(createdMs + i).toISOString(),
        parent_id: null,
        ...(m.tool_calls && m.tool_calls.length > 0
          ? { tool_calls: m.tool_calls }
          : {}),
        ...(m.usage != null ? { usage: m.usage } : {}),
        ...(m.meta != null ? { meta: m.meta } : {}),
      }));
      const messages: ChatMessage[] = mapHistoryItems(
        historyItems,
        detail.root_conversation_id,
      );
      const saModelId =
        typeof detail.model_id === "string" && detail.model_id !== ""
          ? detail.model_id
          : null;
      const saModelProvider =
        typeof detail.model_provider === "string" ? detail.model_provider : "";
      const titleLabel =
        detail.title?.trim() ||
        detail.prompt_preview?.trim() ||
        subagentId;
      this._patchTab(tabId, {
        // Adopt the REAL root conversation id (placeholder was "") so the
        // transport / interrupt / message-bridge paths route correctly. Note:
        // for a grand sub-agent (depth ≥ 2) the tab's `conversationId` still
        // equals the ROOT (the top-of-tree conversation) — the direct parent
        // sub-agent is captured in `subagentMeta.parentSubagentId`.
        conversationId: detail.root_conversation_id,
        title: tab.title || `SubAgent: ${titleLabel}`,
        messages,
        todoList: extractLatestTodoList(messages),
        ...(saModelId !== null
          ? { modelId: saModelId, modelProvider: saModelProvider }
          : {}),
        subagentMeta: {
          subagentId: detail.subagent_id,
          rootConversationId: detail.root_conversation_id,
          parentSubagentId: detail.parent_subagent_id ?? null,
          depth: detail.depth ?? 1,
          status: detail.status,
          owner: detail.owner,
          ...(typeof detail.used_tokens === "number"
            ? { usedTokens: detail.used_tokens }
            : {}),
          ...(typeof detail.budget_tokens === "number"
            ? { budgetTokens: detail.budget_tokens }
            : {}),
          ...(typeof detail.ratio === "number" ? { ratio: detail.ratio } : {}),
          ...(typeof detail.raw_used_tokens === "number"
            ? { rawUsedTokens: detail.raw_used_tokens }
            : {}),
          ...(typeof detail.raw_ratio === "number"
            ? { rawRatio: detail.raw_ratio }
            : {}),
          ...(saModelId !== null
            ? { modelId: saModelId, modelProvider: saModelProvider }
            : {}),
        },
      });
      if (detail.allow_spawn === true) {
        this.setSelfAllowSpawn(tabId, true);
      }
      // SubAgentRail index cache — mirror the freshly-fetched detail into the
      // per-root-conversation index so the rail's chip list is pre-populated
      // BEFORE the user ever switches into this restored tab. Without this,
      // an orphan sub-agent restored from localStorage (parent main tab NOT
      // in the persisted layout) leaves the rail empty until `switchTab`
      // triggers its opportunistic `_fetchSubAgentIndex` fetch — user sees a
      // brief empty-rail flash. Upsert here is precise knowledge (we just
      // decoded the authoritative detail), so it's cheaper AND deterministic
      // vs. queuing another REST list-fetch. Mirrors the same upsert
      // `_openSubAgentTabInner` + `_refreshSubAgentTab` already do on the
      // open / refresh paths, keeping the three sub-agent-detail sinks in
      // sync with the rail's data source.
      this._upsertSubAgentIndexEntry({
        subagentId: detail.subagent_id,
        rootConversationId: detail.root_conversation_id,
        parentSubagentId: detail.parent_subagent_id ?? null,
        depth: detail.depth ?? 1,
        title: titleLabel,
        status: detail.status,
        owner: detail.owner,
        ...(typeof detail.used_tokens === "number"
          ? { usedTokens: detail.used_tokens }
          : {}),
        ...(typeof detail.budget_tokens === "number"
          ? { budgetTokens: detail.budget_tokens }
          : {}),
        ...(saModelId !== null
          ? { modelId: saModelId, modelProvider: saModelProvider }
          : {}),
      });
      // Resubscribe the live stream (cursor-0 replay) — ALWAYS subscribe, the
      // broadcaster decides terminality. A terminal sub-agent replays its
      // full buffer once and closes.
      this._subscribeSubAgentStream(tabId, detail.subagent_id);
    },

    renameTab(tabId: TabId, title: string): void {
      const trimmed = title.trim();
      if (trimmed === "") {
        return;
      }
      this._patchTab(tabId, { title: trimmed });
    },

    /**
     * Rename ALL tabs bound to a given conversation (V2 supports opening the
     * same conversation in multiple tabs). Used by the auto-title and manual
     * rename paths so every tab showing that conversation stays consistent
     * with the sidebar — not just the first one found. No-op for a blank
     * title or when no tab is bound to the conversation.
     */
    renameTabsByConversation(
      conversationId: ConversationId,
      title: string,
    ): void {
      const trimmed = title.trim();
      if (trimmed === "") {
        return;
      }
      for (const tab of this.tabs) {
        if (tab.conversationId === conversationId) {
          this._patchTab(tab.id, { title: trimmed });
        }
      }
    },

    setConversationId(tabId: TabId, conversationId: ConversationId): void {
      this._patchTab(tabId, { conversationId });
      // If this tab already carries an in-memory session tool override (set on
      // a brand-new tab BEFORE it had a conversationId), persist it now that
      // the conversation exists — so "toggle on new tab → send → close" still
      // survives a reopen (keyed by the freshly-assigned conversationId).
      const tab = this.tabs.find((t) => t.id === tabId);
      const o = tab?.sessionToolOverride;
      if (
        o !== undefined &&
        (o.disabledTools.length > 0 || o.disabledSkills.length > 0)
      ) {
        saveSessionToolOverride(String(conversationId), {
          disabledTools: [...o.disabledTools],
          disabledSkills: [...o.disabledSkills],
        });
      }
    },

    /**
     * Unbind a tab from its conversation (reset to a blank tab). Used to
     * self-heal a stale `conversationId` (restored from localStorage) that is
     * absent from a fresh/empty backend DB — see `isConversationNotFound` and
     * the `loadHistoryMessages` / send-error paths (AGENTS.md §🔴
     * State-Truth-First). The tab then behaves like a new blank tab: the next
     * send auto-creates a fresh conversation.
     */
    clearConversationId(tabId: TabId): void {
      this._patchTab(tabId, { conversationId: null });
    },

    /** Toggle the active toolbar mode for a tab.  Mirrors V1's
     *  `activeToolMode = activeToolMode === mode ? null : mode` pattern.
     */
    setActiveMode(tabId: TabId, mode: ToolModeKey | null): void {
      this._patchTab(tabId, { activeMode: mode });
    },

    /** Shallow-merge tool params for a tab (per-mode control changes). */
    setToolParams(tabId: TabId, params: Partial<ToolParams>): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) return;
      this._patchTab(tabId, {
        toolParams: { ...tab.toolParams, ...params },
      });
    },

    /** Replace the tab's modelParams in one transaction. */
    setModelParams(tabId: TabId, params: Partial<ModelParams>): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) return;
      this._patchTab(tabId, {
        modelParams: { ...tab.modelParams, ...params },
      });
    },

    /** Reset modelParams to factory defaults. */
    resetModelParams(tabId: TabId): void {
      this._patchTab(tabId, { modelParams: { ...DEFAULT_MODEL_PARAMS } });
    },

    // ---------------------------------------------------------------
    // Message queue — V1 useChat.js:2820-2840 parity
    // ---------------------------------------------------------------

    /**
     * Enqueue a prompt while the tab is streaming (V1 `handleEnter`
     * enqueue branch, useChat.js:2820-2831). Returns:
     *   - "queued"  : the trimmed text was appended to the queue and the
     *                 panel auto-expanded (V1 `queueExpanded = true`).
     *   - "full"    : the queue already holds MAX_QUEUE_SIZE items; the
     *                 caller surfaces the "queue full" toast.
     *   - "empty"   : the text was blank after trimming → ignored.
     *   - "no-tab"  : the tab id is unknown.
     * The caller (ChatComposer) is responsible for clearing its own
     * textarea + the success toast, mirroring V1 which does so in the
     * same handler.
     *
     * `imagePrefix` (optional) carries already-uploaded image markdown
     * (`![name](url)\n`) captured at enqueue time — see `QueuedMessage`.
     * Stored alongside the text so the re-send replays the image exactly
     * like a fresh image submit. A blank `imagePrefix` with non-blank text
     * is a normal text-only enqueue; a blank text with a non-blank
     * `imagePrefix` (image-only message) still queues.
     */
    enqueueMessage(
      tabId: TabId,
      text: string,
      imagePrefix = "",
    ): "queued" | "full" | "empty" | "no-tab" {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return "no-tab";
      }
      const trimmed = text.trim();
      // An image-only message (blank text but an uploaded image) is valid —
      // only reject when there is NEITHER text NOR an image.
      if (trimmed === "" && imagePrefix === "") {
        return "empty";
      }
      if (tab.messageQueue.length >= MAX_QUEUE_SIZE) {
        return "full";
      }
      this._patchTab(tabId, {
        messageQueue: [
          ...tab.messageQueue,
          { id: nextQueueId(), text: trimmed, imagePrefix },
        ],
        // V1 auto-expands the panel on enqueue so the user sees the
        // pending list grow (useChat.js:2830).
        queueExpanded: true,
      });
      return "queued";
    },

    /**
     * Pop the head of the queue (V1 `messageQueue.value.shift()`,
     * useChat.js:2790). Returns the dequeued item, or null when the
     * queue is empty. Collapses the panel when the queue drains, matching
     * V1's `queueExpanded = false` in the empty branch (useChat.js:2795).
     */
    dequeueMessage(tabId: TabId): QueuedMessage | null {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined || tab.messageQueue.length === 0) {
        if (tab !== undefined && tab.queueExpanded) {
          this._patchTab(tabId, { queueExpanded: false });
        }
        return null;
      }
      const [head, ...rest] = tab.messageQueue;
      this._patchTab(tabId, {
        messageQueue: rest,
        // Collapse once the last item is consumed (V1 useChat.js:2795).
        queueExpanded: rest.length > 0 ? tab.queueExpanded : false,
      });
      return head ?? null;
    },

    /** Remove a single queued message by id (V1 `removeFromQueue`,
     *  useChat.js:2837-2840). Collapses the panel if the queue empties. */
    removeFromQueue(tabId: TabId, queueId: string): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      const next = tab.messageQueue.filter((q) => q.id !== queueId);
      if (next.length === tab.messageQueue.length) {
        return;
      }
      this._patchTab(tabId, {
        messageQueue: next,
        queueExpanded: next.length > 0 ? tab.queueExpanded : false,
      });
    },

    /** Clear the entire queue (V1 useChat.js:2782-2785 — when a stream is
     *  aborted by the user the pending queue is discarded). */
    clearQueue(tabId: TabId): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined || tab.messageQueue.length === 0) {
        return;
      }
      this._patchTab(tabId, { messageQueue: [], queueExpanded: false });
    },

    /**
     * Insert an optimistic mid-turn injection bubble DIRECTLY into the
     * conversation (V2 enhancement — the "inject" button), NOT into the
     * pending send-queue. The bubble renders immediately as a grey / pending
     * `role:user` message (`meta.injected` + `meta.pending`) so the user sees
     * their text fold into the live transcript at once; the backend's
     * `injected_message` frame later RECONCILES it (clears `meta.pending`,
     * pairs by trimmed text — see `handleInjectedMessage`) into a committed
     * bubble, rather than appending a second one.
     *
     * Injection is control-plane-only (user decision 2026-06-24): there is NO
     * queue fallback. If the control WS cannot deliver the `inject` frame the
     * caller removes this pending bubble again (`removePendingInjection`) and
     * surfaces an error toast — the text is never silently re-queued as a
     * fresh turn.
     *
     * Returns the new message id on success, or one of the rejection codes
     * (`empty` / `no-tab`) so the caller can branch.
     */
    insertPendingInjection(
      tabId: TabId,
      text: string,
    ): { result: "inserted"; id: string } | { result: "empty" | "no-tab" } {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return { result: "no-tab" };
      }
      const trimmed = text.trim();
      if (trimmed === "") {
        return { result: "empty" };
      }
      const id = nextMessageId();
      const msg: ChatMessage = {
        id,
        role: "user",
        content: trimmed,
        createdAt: Date.now(),
        ...(tab.conversationId !== null
          ? { conversationId: tab.conversationId }
          : {}),
        meta: { injected: true, pending: true },
      };
      this._patchTab(tabId, { messages: [...tab.messages, msg] });
      return { result: "inserted", id };
    },

    /**
     * Remove an optimistic pending injection bubble by its message id (the
     * control WS could not deliver the `inject` frame, so the text never
     * reached the run). Pairs the `insertPendingInjection` rollback path. Only
     * removes a message that is still `meta.pending === true` (never a bubble
     * the backend already committed via `injected_message`). No-op when the
     * id is unknown / already committed.
     */
    removePendingInjection(tabId: TabId, messageId: string): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      const next = tab.messages.filter(
        (m) =>
          !(
            m.id === messageId &&
            (m.meta as Record<string, unknown> | undefined)?.["pending"] ===
              true
          ),
      );
      if (next.length !== tab.messages.length) {
        this._patchTab(tabId, { messages: next });
      }
    },

    /** Toggle the floating queue panel between expanded list and
     *  collapsed count badge (V1 `queueExpanded` click handlers). */
    setQueueExpanded(tabId: TabId, expanded: boolean): void {
      this._patchTab(tabId, { queueExpanded: expanded });
    },

    /**
     * Recall a queued message back to the composer for editing (the bubble's
     * ✎ "edit" affordance): remove the item from the queue and RETURN its text
     * so the composer can append it to the current draft. Returns `null` when
     * the tab / item is unknown (nothing to recall). Collapses the panel when
     * the queue empties (parity with `removeFromQueue`). This is the "cancel +
     * re-edit" path the user asked for (图一 bubble edit icon): unlike
     * `removeFromQueue` (pure cancel) it hands the text back to the caller.
     */
    recallFromQueue(tabId: TabId, queueId: string): string | null {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return null;
      }
      const item = tab.messageQueue.find((q) => q.id === queueId);
      if (item === undefined) {
        return null;
      }
      const next = tab.messageQueue.filter((q) => q.id !== queueId);
      this._patchTab(tabId, {
        messageQueue: next,
        queueExpanded: next.length > 0 ? tab.queueExpanded : false,
      });
      // Hand back the image markdown prefix (already-uploaded `![](url)`) +
      // text so a recalled image-bearing message is not silently stripped of
      // its image: the prefix re-renders the image on the next send (the URL
      // stays valid). For a text-only item `imagePrefix` is "" → unchanged.
      return `${item.imagePrefix}${item.text}`;
    },

    /**
     * Replace a tab's Multi-Agent discussion config (block-5). Used by
     * `useDiscussion` after a fetch / mutation so the panel, the SSE qs builder,
     * and the frame handlers all read one authoritative copy on the tab. The
     * object is stored as-is (callers pass a fresh object); no merge.
     */
    setDiscussion(tabId: TabId, discussion: DiscussionConfig): void {
      this._patchTab(tabId, { discussion });
    },

    /**
     * Replace the authoritative per-tab implementation-run state (DISC-1
     * §22.9). Mirrors `setDiscussion`: the single source of truth lives on the
     * tab so the ImplementationPanel, the SSE frame handlers
     * (`plan_ready` / `implementation_item_*` / `implementation_phase_changed`)
     * and `useImplementation` all read one copy. The object is stored as-is
     * (callers pass a fresh object); no merge.
     */
    setImplementation(tabId: TabId, implementation: TabImplementationState): void {
      this._patchTab(tabId, { implementation });
    },

    /**
     * Pin a participant to speak on the next turn (multi-agent block-5
     * "call-on"). Forwarded as the `pinned_speaker` SSE query param by the
     * transport. Pass null to clear.
     */
    setPinnedSpeaker(tabId: TabId, participantId: string | null): void {
      this._patchTab(tabId, { pinnedSpeaker: participantId });
    },

    /**
     * Toggle the per-(sub-agent)-tab "allow sub-agent question" switch. When
     * `on` AND this is a sub-agent take-over tab, the transport forwards
     * `allow_question=true` so the backend advertises the blocking `question`
     * tool to the taken-over sub-agent (its dialog is reachable because the
     * user has the tab open). Default `false` keeps `question` excluded
     * (autonomous sub-agent parity). Persisted with the tab so the choice
     * survives reload (see `persistedLayout` / `loadLayout`).
     */
    setSubAgentAllowQuestion(tabId: TabId, on: boolean): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined || tab.kind !== "subagent") return;
      this._patchTab(tabId, { allowSubAgentQuestion: on });
    },

    /**
     * Toggle whether THIS main-agent tab lets its first-level sub-agents spawn
     * their own (second-level / grand) sub-agents (V2 enhancement). Per-tab,
     * session-lifetime state (NOT localStorage-persisted — `persistedLayout`
     * snapshots only `conversationId` + `title`). When on, the transport
     * forwards `allow_child_spawn=true` so the backend grants the `agent`
     * (spawn) tool to the sub-agents this main agent spawns. Default off keeps
     * the historical hard recursion guard (sub-agents cannot spawn). Only
     * meaningful on a depth-0 chat tab; harmless on a sub-agent tab (the
     * transport gates forwarding on `kind`). See `ChatTab.allowChildSpawn`.
     */
    setAllowChildSpawn(tabId: TabId, on: boolean): void {
      this._patchTab(tabId, { allowChildSpawn: on });
    },

    /**
     * Toggle whether THIS taken-over sub-agent may itself create sub-agents
     * (V2 enhancement). Per-tab, session-lifetime state (sub-agent tabs are
     * excluded from `persistedLayout` entirely). When on, the transport
     * forwards `self_allow_spawn=true` so the backend advertises the `agent`
     * (spawn) tool to the taken-over sub-agent. Independent of the main agent's
     * `allowChildSpawn` toggle. Default off keeps the autonomous sub-agent
     * parity (no spawn). Only meaningful for `kind === "subagent"` tabs. See
     * `ChatTab.selfAllowSpawn`.
     */
    setSelfAllowSpawn(tabId: TabId, on: boolean): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined || tab.kind !== "subagent") return;
      this._patchTab(tabId, { selfAllowSpawn: on });
    },

    /**
     * Update THIS tab's away auto-answer settings (V2 enhancement). Strictly
     * per-tab + default-off: merges the given partial over the tab's current
     * settings (or `DEFAULT_AWAY_AUTO_ANSWER` when unset) and clamps
     * `timeoutSeconds` into `[AWAY_AUTO_ANSWER_MIN_SECONDS,
     * AWAY_AUTO_ANSWER_MAX_SECONDS]`. Turning it off here does NOT clear any
     * one-shot per-question suppression (that is reset as new questions
     * arrive). Never touches another tab. Not persisted (ephemeral "I'm away"
     * state — see `ChatTab.awayAutoAnswer`).
     */
    setAwayAutoAnswer(
      tabId: TabId,
      patch: Partial<AwayAutoAnswerSettings>,
    ): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) return;
      const current = tab.awayAutoAnswer ?? DEFAULT_AWAY_AUTO_ANSWER;
      const next: AwayAutoAnswerSettings = {
        enabled: patch.enabled ?? current.enabled,
        timeoutSeconds: patch.timeoutSeconds ?? current.timeoutSeconds,
        prompt: patch.prompt ?? current.prompt,
      };
      // Clamp the timeout defensively so a bad form value can never produce a
      // 0s / negative / absurd countdown.
      if (!Number.isFinite(next.timeoutSeconds)) {
        next.timeoutSeconds = DEFAULT_AWAY_AUTO_ANSWER.timeoutSeconds;
      }
      next.timeoutSeconds = Math.min(
        AWAY_AUTO_ANSWER_MAX_SECONDS,
        Math.max(AWAY_AUTO_ANSWER_MIN_SECONDS, Math.round(next.timeoutSeconds)),
      );
      this._patchTab(tabId, { awayAutoAnswer: next });
    },

    /**
     * Suppress away auto-answer for exactly ONE pending question (the user
     * clicked "don't auto-answer this question" on the active card). Records
     * the originating `question` frame id so its countdown is cancelled, while
     * leaving `awayAutoAnswer.enabled` untouched (the next question still
     * auto-answers). Never touches another tab.
     */
    suppressAwayAutoAnswerForFrame(tabId: TabId, frameId: string): void {
      this._patchTab(tabId, { awayAutoAnswerSuppressedFrameId: frameId });
    },

    /**
     * Set the per-session tool / SKILL override (OVERRIDE/DIFF semantics — see
     * `ChatTab.sessionToolOverride`). The patch carries the COMPLETE current
     * diff (the popover owns the full disabled sets and re-emits them on each
     * change). Passing two empty arrays is equivalent to "follow global
     * defaults" — we normalise that to `undefined` so the outgoing payload
     * omits the fields entirely and the tab's `active` indicator clears.
     * Persisted (keyed by conversationId for a main-agent tab, or by
     * `sub:<subagentId>` for a sub-agent tab — see `_persistKeyForTab`) so the
     * toggles survive closing+reopening the tab AND a full reload. A brand-new
     * unsent main-agent tab (no conversationId yet) keeps it in-memory until
     * the conversation exists (then `setConversationId` flushes it).
     */
    setSessionToolOverride(
      tabId: TabId,
      override: { disabledTools: string[]; disabledSkills: string[] },
    ): void {
      const disabledTools = [...new Set(override.disabledTools)];
      const disabledSkills = [...new Set(override.disabledSkills)];
      const empty = disabledTools.length === 0 && disabledSkills.length === 0;
      this._patchTab(tabId, {
        sessionToolOverride: empty ? undefined : { disabledTools, disabledSkills },
      });
      // Persist under the tab's own key (conversationId, or sub:<id> for a
      // sub-agent tab) so it survives tab close+reopen without colliding with
      // the parent conversation's main-agent override.
      const key = this._persistKeyForTab(tabId);
      if (key !== null) {
        // Sub-agent tabs persist an EXPLICIT tombstone even when empty (all-on),
        // so a user who reset the sub-agent to all-on KEEPS that choice and does
        // NOT silently re-inherit the parent conversation's override on reopen.
        const isSub = this.tabs.find((t) => t.id === tabId)?.kind === "subagent";
        saveSessionToolOverride(
          key,
          empty ? null : { disabledTools, disabledSkills },
          { explicitEmpty: isSub },
        );
      }
    },

    /**
     * Resolve the persistence key for a tab's session tool override:
     *  • sub-agent tab → `sub:<subagentId>` (its own, never the parent conv);
     *  • main-agent tab → its conversationId;
     *  • neither available (brand-new unsent tab) → null (in-memory only).
     */
    _persistKeyForTab(tabId: TabId): string | null {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) return null;
      if (tab.kind === "subagent") {
        const sid = tab.subagentMeta?.subagentId;
        return sid ? _subOverrideKey(sid) : null;
      }
      return tab.conversationId ?? null;
    },

    /** Clear the per-session tool / SKILL override (back to global defaults). */
    resetSessionToolOverride(tabId: TabId): void {
      this._patchTab(tabId, { sessionToolOverride: undefined });
      const key = this._persistKeyForTab(tabId);
      if (key !== null) {
        // For a sub-agent tab, "reset" is still an EXPLICIT user choice (all-on)
        // → persist a tombstone so it does not re-inherit the parent's override
        // on reopen. For a main-agent tab, reset = delete (follow global).
        const isSub = this.tabs.find((t) => t.id === tabId)?.kind === "subagent";
        saveSessionToolOverride(key, null, { explicitEmpty: isSub });
      }
    },


    // ---------------------------------------------------------------
    // 4-state machine — §10.5
    //
    // Two ways to enter the `streaming` status, with DIFFERENT semantics:
    //
    //   `setStreaming(tabId)` — a.k.a. "begin a FRESH turn".
    //     Called at the start of a new user turn / new subscription attach
    //     where NO prior in-memory streaming state should be preserved.
    //     Wipes ALL round-routing indices (`roundMessageIds`,
    //     `roundSubAgentMessageIds`, `activeToolMessageId`,
    //     `activeSubAgentMessageId`, `streamingContent`, sender anchor,
    //     etc.). Legal source states: `idle` (normal) or `error` — on `error`
    //     sources `lastError` is wiped as part of the fresh-turn reset (the
    //     caller does NOT need to pre-clear it). Calling on `streaming` is
    //     idempotent (the wipe is safe because "fresh" is what the caller
    //     asked for) but almost always the wrong tool — use `resumeStreaming`
    //     instead when RE-attaching to an already in-flight turn.
    //
    //   `resumeStreaming(tabId)` — a.k.a. "RE-ATTACH to an in-flight turn".
    //     Called when a broadcaster/transport RE-SUBSCRIBES to a stream
    //     that was already in progress (the user switched away and back;
    //     a WS drop was recovered; a `subagent_start` woke a standalone
    //     tab whose PRIOR run had not yet terminated). MUST NOT wipe
    //     round-routing — the next chunk frame carries the SAME
    //     `round_index` as the message it was streaming into before the
    //     re-attach, and needs `roundMessageIds[ri]` intact so
    //     `handleChunk` can append instead of opening a spurious new
    //     assistant bubble ("切走切回断段" bug, historically root-caused
    //     to `setStreaming`'s wipe on `_subscribeSubAgentStream`'s first
    //     frame). Only touches the state-machine fields (`status`,
    //     `lastError`, `networkRetry`); leaves all round routing intact.
    //     Idempotent on `streaming` / `aborting`. Refuses to promote from
    //     terminal `error` (caller must first `setStreaming` — that IS a
    //     fresh turn).
    //
    // Historical background: `setStreaming` was originally the only
    // entry, and the wipe was baked in because it doubled as "start of a
    // new turn — clear everything". Callers that meant "re-attach"
    // (`_subscribeSubAgentStream.ensureStreaming`; `applyFrame`'s
    // cross-tab wake for `subagent_start`) were forced to use it and
    // silently clobbered live round-routing, causing the reported
    // multi-attempt断段 bug. `resumeStreaming` was extracted 2026-07-14
    // to break this semantic overload — see
    // `chatTabs.subagent-resubscribe-preserves-routing.spec.ts` for the
    // regression.
    // ---------------------------------------------------------------

    setStreaming(tabId: TabId): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      // "Fresh turn" semantics — wipe ALL streaming-transient state.
      // Callers that mean "re-attach to an already-in-flight turn" MUST
      // use `resumeStreaming` instead (see doc block above).
      this._patchTab(tabId, {
        status: "streaming",
        lastError: null,
        streamingContent: "",
        streamingToolCalls: [],
        activeToolMessageId: null,
        roundMessageIds: {},
        roundSubAgentMessageIds: {},
        activeSubAgentMessageId: null,
        streamingUsage: null,
        streamingPerf: null,
        streamingSubAgentBlocks: [],
        streamingRequestId: null,
        networkRetry: null,
        // A fresh turn starts not-compacting; the `compaction_progress`
        // frame (if any) will flip it on/off during the run.
        compacting: false,
        // A fresh turn starts with no outstanding question (any prior one was
        // answered / aborted). `todoList` is intentionally NOT reset — the
        // task list persists across turns so the model can update it
        // incrementally over a multi-turn task (V2 enhancement).
        pendingQuestion: null,
        // Multi-Agent discussion (block-5): clear the live speaker anchor at the
        // start of every turn; the first `speaker_changed` frame sets it.
        streamingSenderId: null,
        streamingSenderName: null,
        streamingSenderColor: null,
        streamingSenderModelId: null,
      });
    },

    /** Re-attach to an already-in-flight streaming turn WITHOUT wiping
     *  round-routing state. See the doc block above `setStreaming` for
     *  the full fresh-vs-resume semantics rationale.
     *
     *  Behavior by source state:
     *    - `streaming`: no-op (already there). Do NOT patch anything —
     *      any spurious patch would still schedule a reactive
     *      recomputation for downstream consumers.
     *    - `aborting`: no-op — a stop is in progress; the terminal frame
     *      will drive `confirmAbort`, do NOT flip back to `streaming`.
     *    - `idle`: promote to `streaming` and clear `lastError` /
     *      `networkRetry` (state-machine housekeeping), but leave every
     *      round-routing field intact. This is the common re-subscribe
     *      case where the prior run terminated cleanly (status went idle
     *      via `confirmDone`) and a NEW run's `subagent_start` is about
     *      to wake this tab — the fresh run needs a streaming status but
     *      the wipe is already done (confirmDone did it), so we do NOT
     *      re-wipe here (a second wipe is harmless but semantically
     *      misleading — we are RESUMING, not starting fresh).
     *    - `error`: refuse (return without patching). A tab in error
     *      state has a persisted `lastError` the user needs to see;
     *      resuming implicitly would eat it. If the caller genuinely
     *      wants to restart from error, it must call `setStreaming`
     *      explicitly (fresh turn semantics).
     */
    resumeStreaming(tabId: TabId): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      if (tab.status === "streaming" || tab.status === "aborting") {
        return;
      }
      if (tab.status === "error") {
        // Do NOT silently promote error → streaming. `error` is a terminal
        // state a user must see; a caller wanting to restart from error
        // must explicitly call `setStreaming` (fresh turn).
        return;
      }
      // status === "idle" — promote to streaming without wiping routing.
      this._patchTab(tabId, {
        status: "streaming",
        lastError: null,
        networkRetry: null,
      });
    },

    appendStreamingChunk(tabId: TabId, chunk: string, backfill = false): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      // Only meaningful while streaming; ignore otherwise.
      if (tab.status !== "streaming") {
        return;
      }
      // Perf (V1 useChat.js:446-463 + 1069-1097 parity): do NOT write
      // `streamingContent` per token — that re-runs the full
      // `renderMarkdown(streamingContent)` (marked + highlight.js + DOMPurify
      // over the whole accumulated text) on every chunk, which is O(n²) in the
      // generated length and makes long model-build answers crawl. Instead
      // buffer the chunk and flush ALL pending text into reactive state once
      // per animation frame, capping re-renders to ≤~60/s regardless of token
      // rate. Terminal transitions (done/abort/error) flush synchronously.
      //
      // `backfill === true` (the chunk is part of a broadcaster cursor=0 replay
      // — sub-agent tab cold-open with broadcaster entry still alive, or
      // active-run WS attach) accumulates into the same coalescing buffer but
      // SUPPRESSES the scheduled flush: a backfill chunk represents text the
      // user has already seen rendered (via the HTTP snapshot), so committing
      // it逐 frame would replay-by-typewriter the entire trailing transcript.
      // The buffer keeps growing; the backfill→live boundary (or terminal
      // close) in `handleFrame` calls `flushStreamingNow` to commit the whole
      // batch in ONE reactive write.
      bufferStreamingChunk(tabId, chunk);
      scheduleStreamingFlush(
        tabId,
        (id) => {
          this._flushStreamingBuffer(id);
        },
        // Tiered flush (perf): the active/foreground tab keeps per-frame flush
        // (glassy streaming, unchanged); a background streaming tab downshifts
        // to a coarser timer so concurrent agentic sessions don't saturate the
        // main thread with per-frame reactive writes the user isn't watching.
        tabId === this.activeTabId,
        backfill,
      );
    },

    /**
     * Flush coalesced streaming chunks into reactive `streamingContent` in a
     * single patch. Called from the rAF scheduler and synchronously from every
     * terminal transition (confirmDone / confirmAbort / recordError) so no
     * trailing text is dropped. No-op when nothing is buffered.
     */
    _flushStreamingBuffer(tabId: TabId): void {
      if (!hasBufferedStreamingChunk(tabId)) {
        return;
      }
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        // Tab vanished — drop the buffer so it can't leak into a later tab.
        clearStreamingBuffer(tabId);
        return;
      }
      const pending = takeBufferedStreamingChunk(tabId);
      if (tab.status !== "streaming" && tab.status !== "aborting") {
        // Stream already settled to a terminal (idle/error) state; discard
        // late text rather than resurrecting a terminal tab's bubble.
        return;
      }
      this._patchTab(tabId, {
        streamingContent: tab.streamingContent + pending,
      });
    },

    /**
     * Synchronously flush any rAF-buffered streaming text into reactive
     * `streamingContent` now. Public so callers (and tests) can force the
     * coalesced text to be observable without waiting for the next frame.
     */
    flushStreamingNow(tabId: TabId): void {
      cancelStreamingFlush(tabId);
      this._flushStreamingBuffer(tabId);
    },

    /**
     * Append a CHUNK's text to a SPECIFIC round message's `content` (the
     * `round_index` zero-inference path). Unlike `appendStreamingChunk` (which
     * feeds the single bottom `streamingContent` buffer), this binds the text to
     * THAT round's message so multi-round narration never accumulates into one
     * block below the tool cards (the reported "云端多轮文本沉底" bug).
     *
     * rAF coalescing is preserved (same perf budget as the bottom buffer): the
     * chunk is buffered per-tab keyed to its target round message; a switch to a
     * DIFFERENT round message flushes the prior round's buffer FIRST (so each
     * round's text stays bound to its own message), then the new round starts
     * buffering. The reactive write into the round message's `content` happens
     * once per animation frame.
     */
    appendRoundChunk(
      tabId: TabId,
      messageId: string,
      chunk: string,
      backfill = false,
    ): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      if (tab.status !== "streaming") {
        return;
      }
      // Round switch → flush the previous round's buffer to ITS message first.
      const priorTarget = peekRoundChunkTarget(tabId);
      if (priorTarget !== null && priorTarget !== messageId) {
        cancelRoundChunkFlush(tabId);
        this._flushRoundChunkBuffer(tabId);
      }
      bufferRoundChunk(tabId, messageId, chunk);
      // ── Backfill (transcript-so-far) suppression ────────────────────────
      // `backfill === true` means this chunk is part of a broadcaster cursor=0
      // replay (sub-agent cold-open / active-run WS attach reconnect): the
      // user has already seen this text rendered via the HTTP snapshot. We
      // accumulate into the buffer above but SUPPRESS the scheduled flush —
      // `handleFrame` commits the whole accumulated batch in ONE reactive
      // write at the backfill→live boundary (or terminal). Without this guard
      // each backfill chunk frame triggered an individual `flushRoundChunkNow`
      // (one synchronous commit each) → user-visible逐段重播 of the trailing
      // transcript. Live chunks (backfill=false) keep the existing per-rAF
      // tier so真正 live streaming is unchanged.
      scheduleRoundChunkFlush(
        tabId,
        (id) => {
          this._flushRoundChunkBuffer(id);
        },
        // Tiered flush (perf): see appendStreamingChunk — active tab per-frame,
        // background tab downshifted.
        tabId === this.activeTabId,
        backfill,
      );
    },

    /**
     * Flush the coalesced per-round chunk buffer into its target round message's
     * `content` in a single patch. Called from the rAF scheduler, on a round
     * switch (in `appendRoundChunk`), and synchronously before every non-chunk
     * frame / terminal transition so no round text is dropped or mis-ordered.
     * No-op when nothing is buffered or the target message no longer exists.
     */
    _flushRoundChunkBuffer(tabId: TabId): void {
      if (!hasBufferedRoundChunk(tabId)) {
        return;
      }
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        clearStreamingBuffer(tabId);
        return;
      }
      const pending = takeBufferedRoundChunk(tabId);
      if (pending === null || pending.text === "") {
        return;
      }
      if (tab.status !== "streaming" && tab.status !== "aborting") {
        // Settled to a terminal state — discard late text rather than
        // resurrecting a terminal tab's message.
        return;
      }
      const idx = tab.messages.findIndex((m) => m.id === pending.messageId);
      if (idx < 0) {
        // Target round message vanished (should not happen — it is created
        // before buffering). Drop rather than fabricate.
        return;
      }
      const target = tab.messages[idx]!;
      const nextMsg: ChatMessage = {
        ...target,
        content: target.content + pending.text,
      };
      this._patchTab(tabId, {
        messages: [
          ...tab.messages.slice(0, idx),
          nextMsg,
          ...tab.messages.slice(idx + 1),
        ],
      });
    },

    /**
     * Synchronously flush any rAF-buffered per-round text into its round
     * message's `content` now. Public so callers/tests can force the coalesced
     * text observable without waiting for the next frame.
     */
    flushRoundChunkNow(tabId: TabId): void {
      cancelRoundChunkFlush(tabId);
      this._flushRoundChunkBuffer(tabId);
    },

    /**
     * Absorb a streaming tool-card output `delta` into the card's BOUNDED
     * preview buffer (frozen head + rolling tail; middle folded) and schedule a
     * THROTTLED reactive write of the rendered preview into the card's `output`
     * (at most one paint per animation frame, tiered like the chunk buffers).
     *
     * Perf root-cause fix: a chatty tool (exec emitting 万行/MB of output) used
     * to do `output = output + delta` + replace the whole `messages` array per
     * `partial` frame → O(n) concat + O(n) re-render per frame → O(n²) over the
     * stream, and the card held the entire multi-MB string. Here the data is
     * absorbed into a non-reactive bounded buffer (O(delta), capped size) and
     * the reactive write is coalesced — so neither memory nor render cost grows
     * without bound. The complete output is persisted by the backend and
     * retrievable via the `read` tool. Returns the current bounded preview so a
     * freshly-seeded card can store it synchronously.
     */
    bufferToolOutput(tabId: TabId, cardKey: string, delta: string): string {
      const preview = bufferToolOutputDelta(tabId, cardKey, delta);
      scheduleToolOutputFlush(
        tabId,
        (id) => {
          this._flushToolOutputBuffer(id);
        },
        tabId === this.activeTabId,
      );
      return preview;
    },

    /**
     * Flush every dirty tool-card preview for a tab into its card's reactive
     * `output` in a SINGLE messages-array patch (one paint for all cards that
     * absorbed deltas since the last flush). Called from the throttle scheduler
     * and synchronously before non-chunk frames / terminal transitions. No-op
     * when nothing is dirty.
     */
    _flushToolOutputBuffer(tabId: TabId): void {
      const keys = dirtyToolOutputKeys(tabId);
      if (keys.length === 0) {
        return;
      }
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        cancelToolOutputFlush(tabId);
        return;
      }
      if (tab.status !== "streaming" && tab.status !== "aborting") {
        // Settled to a terminal state — drop late previews rather than
        // resurrecting a terminal tab's card.
        return;
      }
      // Map each dirty card key → its latest bounded preview, then rebuild the
      // messages array ONCE, patching every matching card's `output`.
      const previews = new Map<string, string>();
      for (const key of keys) {
        const text = takeDirtyToolOutput(tabId, key);
        if (text !== null) previews.set(key, text);
      }
      if (previews.size === 0) {
        return;
      }
      let mutated = false;
      const messages = tab.messages.map((m) => {
        const cards = m.toolCalls;
        if (cards === undefined || cards.length === 0) return m;
        let msgChanged = false;
        const nextCards = cards.map((c) => {
          const preview = previews.get(c.callId ?? c.id);
          if (preview === undefined || preview === c.output) return c;
          msgChanged = true;
          return { ...c, output: preview };
        });
        if (!msgChanged) return m;
        mutated = true;
        return { ...m, toolCalls: nextCards };
      });
      if (mutated) {
        this._patchTab(tabId, { messages });
      }
    },

    /**
     * Synchronously flush + DROP a single tool card's bounded-preview buffer
     * (its stream settled). Flushing the whole tab's pending previews first
     * keeps any sibling cards' live text from being lost, then the settled
     * card's buffer is dropped so a pending throttle can't overwrite its
     * canonical output and a reused card key starts fresh.
     */
    flushToolOutput(tabId: TabId, cardKey: string): void {
      cancelToolOutputFlush(tabId);
      this._flushToolOutputBuffer(tabId);
      clearToolOutputBuffer(tabId, cardKey);
    },

    /**
     * Synchronously flush any throttled tool-card previews into their cards'
     * reactive `output` now (ignoring the tier). Public so callers / tests can
     * force the coalesced preview observable without waiting for the next
     * animation frame — mirrors `flushStreamingNow` / `flushRoundChunkNow`.
     */
    flushToolOutputNow(tabId: TabId): void {
      cancelToolOutputFlush(tabId);
      this._flushToolOutputBuffer(tabId);
    },

    /**
     * Apply a server `frame` to the active turn.
     *
     * Dispatches by `frame_type` (api-contract §3 / §4):
     *   - `chunk`            → append assistant text
     *   - `tool_call`        → push a running ChatToolCall (paired by frame_id)
     *   - `tool_result`      → complete the matching ChatToolCall (by tool name)
     *   - `end`              → capture `payload.usage` for the turn
     *   - `tool_mode_changed`→ sync `activeMode` from server-side auto-detect
     *                          (V1 useChat.js:1324-1332 parity — backend
     *                          detected a complex intent like model-build
     *                          from the user message; toolbar must follow so
     *                          subsequent turns also carry the correct mode).
     *   - `error`            → handled by the transport (recordError); ignored
     *
     * `backfill` (default `false`) is the broadcaster `backfill` flag carried
     * on the envelope when this frame is part of a cursor=0 replay of the
     * buffered transcript-so-far (sub-agent cold-open while the broadcaster
     * entry is still alive, or the active-run WS attach path). Live frames
     * MUST pass `false`. The handlers receive it via `ctx.isBackfill` and
     * route text appends through the coalescing-buffer path that SUPPRESSES
     * per-frame flushes — `handleFrame`'s callers are responsible for issuing
     * one synchronous `flushRoundChunkNow`/`flushStreamingNow` at the
     * backfill→live boundary (or terminal close) so the whole historical
     * burst lands as ONE reactive write. This is the fix for "切到一个 done
     * 子 Agent / 接管 active-run 时最后一段历史被打字机重播" bug: backfill is
     * transcript the user has already seen rendered via the snapshot, it must
     * not be played-back逐段.
     */
    applyFrame(
      tabId: TabId,
      frame: ChatStreamFrame,
      backfill = false,
    ): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined || tab.status !== "streaming") {
        // Observability (State-Truth-First): a frame representing REAL backend
        // activity is being dropped because the tab is no longer streaming
        // (e.g. flipped to "error"/"idle" by a mid-turn reconnect / spurious
        // terminal). Previously this was a SILENT `return` with no trace — the
        // root-cause of "a long tool ran 558 s but its card never appeared
        // live, only after reload". Warn so the swallow is never invisible
        // again. Control flow is unchanged (still returns).
        if (tab !== undefined) {
          console.warn(
            "[chatTabs] applyFrame dropped a frame: tab not streaming",
            {
              tabId: String(tabId),
              status: tab.status,
              frameType: frame.frame_type,
            },
          );
        }
        return;
      }
      // F3 cohesion split: dispatch by `frame_type` into the per-kind
      // reducer table (`chatTabs/frameHandlers.ts`). Each handler receives
      // the current tab snapshot + bound action callbacks; unknown / error
      // frame_types have no entry and are ignored (error → recordError via
      // the transport). Behaviour is identical to the previous inline
      // switch — only relocated.
      const handler = FRAME_HANDLERS[frame.frame_type];
      if (handler === undefined) return;
      // Ordering guarantee: `chunk` frames are rAF-coalesced (perf), but every
      // OTHER frame kind (tool_call / tool_result / end / …) must observe the
      // up-to-date `streamingContent`. tool_call, for instance, RESETS
      // streamingContent (anti-"文本重复两遍" in the agentic loop); if buffered
      // chunks flushed AFTER that reset they'd corrupt the next round. So flush
      // the buffer synchronously before any non-chunk frame, then re-read the
      // tab so the handler sees the merged content.
      let current = tab;
      if (frame.frame_type !== "chunk") {
        // Flush BOTH coalescing buffers synchronously so the handler observes
        // up-to-date state: the bottom `streamingContent` (legacy / final
        // summary) AND the per-round buffer (each round message's content).
        // tool_call, for instance, reads/clears `streamingContent` and appends
        // cards onto round messages; a buffered round chunk that flushed AFTER
        // would corrupt ordering.
        cancelStreamingFlush(tabId);
        this._flushStreamingBuffer(tabId);
        cancelRoundChunkFlush(tabId);
        this._flushRoundChunkBuffer(tabId);
        current = this.tabs.find((t) => t.id === tabId) ?? tab;
        if (current.status !== "streaming") {
          // Same observability as the entry guard: the synchronous buffer
          // flush above settled the tab out of "streaming" (a terminal frame
          // raced in), so this non-chunk frame is dropped. Warn (control flow
          // unchanged) so the swallow leaves a trace.
          console.warn(
            "[chatTabs] applyFrame dropped a non-chunk frame: tab left streaming during flush",
            {
              tabId: String(tabId),
              status: current.status,
              frameType: frame.frame_type,
            },
          );
          return;
        }
      }
      handler(current, frame, {
        patchTab: (patch) => this._patchTab(tabId, patch),
        appendStreamingChunk: (text, bf) =>
          this.appendStreamingChunk(tabId, text, bf ?? false),
        appendRoundChunk: (messageId, text, bf) =>
          this.appendRoundChunk(tabId, messageId, text, bf ?? false),
        setActiveMode: (mode) => this.setActiveMode(tabId, mode),
        nextMessageId,
        bufferToolOutput: (cardKey, delta) =>
          this.bufferToolOutput(tabId, cardKey, delta),
        flushToolOutput: (cardKey) => this.flushToolOutput(tabId, cardKey),
        // SubAgentRail index cache — bound so `handleSubagentStart` /
        // `handleSubagentDone` / `handleSubagentError` can surface a
        // newly-spawned sub-agent on the rail IMMEDIATELY (grey/running
        // chip) + refresh terminal status when the sub-agent finishes.
        // See `SubAgentIndexEntry` docstring for the full lifecycle.
        upsertSubAgentIndexEntry: (entry) =>
          this._upsertSubAgentIndexEntry(entry),
        updateSubAgentIndexStatus: (subagentId, status) =>
          this._updateSubAgentIndexStatus(subagentId, status),
        isBackfill: backfill,
      });

      // Cross-tab re-subscribe on sub-agent (re)start. A standalone sub-agent
      // tab subscribes to the LIVE stream when opened, but that WS closes when
      // the sub-agent's run hits terminal. When the main agent later RESUMES
      // the SAME sub-agent (`resume_subagent_id` — runs 2..N), a fresh
      // `subagent_start` arrives HERE on the parent conversation's stream
      // carrying that same `subagent_id`; the already-open standalone tab would
      // otherwise stay frozen on the previous run's snapshot (the reported
      // "resume 之后子 Agent 标签页不更新" bug). So whenever a `subagent_start`
      // names a sub-agent that has an open standalone tab, re-subscribe that
      // tab to the new live stream (its cursor-0 replay rebuilds the fresh run).
      // Guard: only act on the parent stream (not when this very frame is being
      // applied to the standalone tab itself), and never re-subscribe the tab
      // to its own in-progress turn.
      if (frame.frame_type === "subagent_start") {
        const sid =
          frame.payload !== null &&
          typeof frame.payload === "object" &&
          typeof (frame.payload as Record<string, unknown>).subagent_id ===
            "string"
            ? ((frame.payload as Record<string, unknown>)
                .subagent_id as string)
            : "";
        if (sid !== "") {
          // Wake any standalone sub-agent tab already open for this id so it
          // starts consuming the live stream. In the β flat-strip model the
          // sub-agent tab may not exist yet — the user opens it explicitly
          // via `openSubAgentTab`; the SubAgentBlock inside the parent's
          // message list shows the sub-agent's progress inline meanwhile.
          const standalone = this.tabs.find(
            (t) =>
              t.kind === "subagent" &&
              t.subagentMeta?.subagentId === sid &&
              t.id !== tabId,
          );
          if (standalone !== undefined) {
            // Delegates fresh-vs-resume decision to
            // `_subscribeSubAgentStream.ensureStreaming` → `resumeStreaming`
            // (2026-07-14 semantic split). Two cases converge safely here:
            //   • Prior run already terminated (`status === "idle"`, the
            //     common resume-with-`resume_subagent_id` path): the first
            //     live frame promotes idle → streaming, preserving whatever
            //     empty routing was set by `confirmDone` — behaviour
            //     unchanged.
            //   • Prior run had not yet delivered its terminal frame
            //     (`status === "streaming"`, rare race): `resumeStreaming`
            //     is a no-op, KEEPING the prior run's routing intact so the
            //     next chunk lands on the correct pre-existing message —
            //     exactly the same fix as the switch-away-and-back bug.
            this._subscribeSubAgentStream(standalone.id, sid);
          }
        }
      }
    },

    /** Record the client-side perf summary for the in-flight turn
     *  (computed by the transport — ttft / total / tool_rounds). */
    setStreamingPerf(tabId: TabId, perf: ChatMessagePerf): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined || tab.status !== "streaming") {
        return;
      }
      this._patchTab(tabId, { streamingPerf: perf });
    },

    /** V1 useChat.js:2270-2292 parity — surface / clear the network
     *  retry banner state for a tab. The transport calls this with a
     *  `{current, max}` snapshot before each retry attempt and with
     *  `null` once the stream succeeds or retries exhaust (the latter
     *  falls through to the regular error path via `recordError`).
     *
     *  Allowed in any status: the banner survives a streaming → error
     *  flip (so the user still sees the retry counter while we transition)
     *  and is cleared explicitly by the transport — never spuriously by
     *  state-machine transitions. */
    setNetworkRetry(tabId: TabId, state: NetworkRetryState | null): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      this._patchTab(tabId, { networkRetry: state });
    },

    /** Dismiss a pending budget-exceeded decision on a tab.
     *
     *  Called when the user resolves the interactive "已达 Token 预算上限"
     *  dialog (`BudgetDecisionDialog`) — either by pressing 「停止」 (leave
     *  the turn stopped) or 「继续」 (raise the cap and resume), or when the
     *  user cancels the budget cap entirely via `BudgetPopover`
     *  (`saveBudget(null)`) making the pending decision moot.
     *
     *  Root-cause fix (2026-07-14, user report "取消 token 上限后切走切回
     *  弹窗又自动弹出来"): both `budgetExceededSignal` and `budgetDecision`
     *  had NO clearing path — once written by `handleEnd` (frameHandlers.ts
     *  `handleEnd` on `reason: "budget_exceeded"`), they lived forever on
     *  the tab. The composer's watcher on `activeTab?.budgetExceededSignal`
     *  fires on tab switch (`activeTab` is a Pinia getter — its identity
     *  changes when `activeTabId` flips), and because `prev` gets polluted
     *  by the sub-agent tab's `undefined` signal on switch-away, the
     *  switch-back sees `prev=undefined, next=<sig>` → guard passes →
     *  `budgetDecision` is still there → dialog re-opens. Documented
     *  intent in `_chatTabsTypes.ts` (the `budgetDecision` field's doc:
     *  "on 'stop' it just clears") matches this action's behaviour —
     *  implementation just never actually cleared.
     *
     *  Clearing BOTH fields together is important:
     *    • Clearing only `budgetDecision` would leave the watcher's
     *      `signal` compare still tripping on tab switch, but then the
     *      `decision !== undefined` branch would fail → fall through to
     *      the legacy toast (`chat.budget.exceededToast`) → user gets a
     *      re-appearing toast instead of a re-appearing dialog: the same
     *      class of bug, less severe.
     *    • Clearing only `budgetExceededSignal` (leaving `budgetDecision`)
     *      would also work for the observed symptom, but leaves stale
     *      decision data on the tab that could be surfaced by any future
     *      code path reading it (defensive: clear the whole tuple).
     *
     *  A subsequent NEW budget hit re-arms both fields via a fresh
     *  `handleEnd` call (`Date.now()` is strictly greater than the prior
     *  value; comparison against `undefined` also passes) — so this
     *  dismiss action never blocks a legitimate future dialog. */
    dismissBudgetDecision(tabId: TabId): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      // Fast path: nothing to dismiss (avoid a redundant reactive write
      // that would still schedule downstream recomputation).
      if (
        tab.budgetExceededSignal === undefined &&
        tab.budgetDecision === undefined
      ) {
        return;
      }
      this._patchTab(tabId, {
        budgetExceededSignal: undefined,
        budgetDecision: undefined,
      });
    },

    /**
     * Answer the tab's pending blocking `question` tool call (V2 enhancement).
     *
     * Clears `tab.pendingQuestion` immediately (so the dialog closes and a
     * stop/abort race never re-opens it) and POSTs the answer to
     * `/api/chat/answer`, which resolves the future the suspended `question`
     * tool handler awaits — the agentic loop then resumes. Fire-and-forget +
     * idempotent: a failed / already-resolved answer is a benign no-op (the
     * server returns `delivered:false`; the question may have timed out).
     */
    answerQuestion(tabId: TabId, answer: string): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined || tab.pendingQuestion === null) {
        return;
      }
      this._patchTab(tabId, { pendingQuestion: null });
      // Control-plane fast path (root-cause of the "438s answer hang"): send
      // the answer over the page-global control WebSocket, which is NOT
      // subject to the browser's HTTP/1.1 6-connection pool — so it can never
      // be queued behind the SSE data streams that saturate that pool when
      // several tabs stream at once. Falls back to the locked REST endpoint
      // when the WS is not ready (initial page-load race / reconnect window /
      // tests). Both paths hit the SAME server-side `question_registry.resolve`
      // with identical idempotency, so the fallback is behaviour-preserving.
      // The control-channel composable is statically imported at module scope
      // (it is already in this chunk via useChatTransport/ChatView, so a
      // dynamic import gains no code-split). The `@/api` barrel stays lazily
      // imported to keep the store decoupled from it (SSR / test isolation).
      const sendViaRest = (): void => {
        void import("@/api")
          .then(({ apiJson }) =>
            apiJson("POST", "/api/chat/answer", {
              tab_id: tabId,
              answer,
            }),
          )
          .catch(() => {
            // ignore — answering is best-effort; the question handler also has
            // a hard timeout + abort fallback so a lost answer never wedges.
          });
      };
      try {
        if (!useChatControlChannel().sendAnswer(tabId, answer)) {
          sendViaRest();
        }
      } catch {
        sendViaRest();
      }
    },

    /** Cancel ONE running tool call (per-call stop) WITHOUT aborting the turn.
     *
     *  Sends a ``cancel_tool`` control message (WS fast path, same reasoning as
     *  ``answerQuestion``) with a REST fallback to ``POST /api/chat/cancel_tool``.
     *  The backend cancels just that tool, synthesizes a ``[cancelled]``
     *  tool_result (fed back to the model) and lets the turn CONTINUE — so we do
     *  NOT touch the tab's ``streaming``/``aborting`` state here. The tool card
     *  settles to a cancelled state when the backend's terminal ``cancelled``
     *  tool_result frame arrives (state-truth-first). Best-effort + idempotent.
     */
    cancelToolCall(tabId: TabId, callId: string): void {
      const cid = (callId ?? "").trim();
      if (cid === "") {
        return;
      }
      const sendViaRest = (): void => {
        void import("@/api")
          .then(({ apiJson }) =>
            apiJson("POST", "/api/chat/cancel_tool", {
              tab_id: tabId,
              call_id: cid,
            }),
          )
          .catch(() => {
            // ignore — per-call cancel is best-effort.
          });
      };
      try {
        if (!useChatControlChannel().sendCancelTool(tabId, cid)) {
          sendViaRest();
        }
      } catch {
        sendViaRest();
      }
    },

    /** Clear a tab's pending question without answering (turn ended / aborted
     *  / tab closed). The server-side handler observes the abort/timeout and
     *  unwinds on its own, so this is a pure UI reset. */
    clearPendingQuestion(tabId: TabId): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined || tab.pendingQuestion === null) {
        return;
      }
      this._patchTab(tabId, { pendingQuestion: null });
    },

    /** Toggle the floating task-list panel between expanded list and the
     *  collapsed count badge (V2 enhancement; mirrors `setQueueExpanded`). */
    setTodoExpanded(tabId: TabId, expanded: boolean): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      this._patchTab(tabId, { todoExpanded: expanded });
    },

    /**
     * Drop an expired prompt-snapshot ``request_id`` from every message that
     * carried it (V1 useForgeConfig.js:64-76 parity). Called when the
     * `/api/prompt-snapshot/{id}` fetch returns 404 — the snapshot lives only
     * in backend memory and is gone after a restart. Removing the id makes the
     * 📄 prompt-snapshot button (which gates on ``meta.request_id``) stop
     * showing on both the assistant message header and its tool cards, instead
     * of lingering and flashing the dialog open→404→closed on every click
     * (§State-Truth-First: the button must reflect whether the snapshot REALLY
     * still exists). Scans all tabs so a snapshot opened from any tab is
     * cleared everywhere it appears.
     */
    clearRequestId(requestId: string): void {
      if (!requestId) {
        return;
      }
      for (const tab of this.tabs) {
        let changed = false;
        const messages = tab.messages.map((m) => {
          const meta = m.meta as Record<string, unknown> | undefined;
          if (meta?.["request_id"] === requestId) {
            changed = true;
            const nextMeta = { ...meta };
            delete nextMeta["request_id"];
            return { ...m, meta: nextMeta };
          }
          return m;
        });
        if (changed) {
          this._patchTab(tab.id, { messages });
        }
      }
    },

    /**
     * Confirm the turn finished (server `done`). Commits accumulated
     * streamingContent into a new assistant ChatMessage. Refuses to
     * transition unless current status is `streaming` (§10.6 inv 4).
     */
    confirmDone(tabId: TabId): void {
      const found = this.tabs.find((t) => t.id === tabId);
      if (found === undefined) {
        return;
      }
      if (found.status !== "streaming") {
        // Idempotent — server may double-emit done after error/abort
        return;
      }
      // Flush any rAF-buffered streaming text before committing so the
      // assistant message captures the FULL generated content. Re-read the
      // tab afterwards: `_patchTab` replaces the object, so the pre-flush
      // reference would carry stale `streamingContent`.
      cancelStreamingFlush(tabId);
      this._flushStreamingBuffer(tabId);
      cancelRoundChunkFlush(tabId);
      this._flushRoundChunkBuffer(tabId);
      const tab = this.tabs.find((t) => t.id === tabId) ?? found;
      const messages = buildConfirmDoneMessages(tab, nextMessageId());
      this._patchTab(tabId, {
        status: "idle",
        streamingContent: "",
        streamingToolCalls: [],
        activeToolMessageId: null,
        roundMessageIds: {},
        roundSubAgentMessageIds: {},
        activeSubAgentMessageId: null,
        streamingUsage: null,
        streamingPerf: null,
        streamingSubAgentBlocks: [],
        streamingRequestId: null,
        compacting: false,
        messages,
        lastError: null,
        networkRetry: null,
        streamingSenderId: null,
        streamingSenderName: null,
        streamingSenderColor: null,
        streamingSenderModelId: null,
      });
      // Promote-ready detection refresh (migration 057): the backend re-detects
      // + persists ``Conversation.detected_model`` at THIS turn's end
      // (``_finalize_assistant_message``). Re-read the conversation summary so
      // the tab's ``detectedModel`` reflects it and the promote CTA updates
      // this turn (the model may have only just produced a promotable variant,
      // or switched to a different model dir). Fire-and-forget + best-effort:
      // never blocks or throws out of the terminal transition.
      void this._refreshDetectedModel(tabId);
    },

    /**
     * Re-read a tab's conversation ``detected_model`` from the backend summary
     * and patch ``tab.detectedModel`` (promote-ready detection, migration 057).
     *
     * Called after a turn ends (``confirmDone``) since the backend re-detects
     * + persists at turn end. Best-effort: a missing conversation id, a
     * transient fetch failure, or the tab going away are all swallowed so it
     * never disturbs the turn-completion path. Never overwrites a tab that
     * switched conversations mid-flight (re-checks the id after the await).
     */
    async _refreshDetectedModel(tabId: TabId): Promise<void> {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) return;
      const convId = tab.conversationId;
      if (convId === null || convId === "") return;
      try {
        const { apiJson } = await import("@/api");
        const summary = await apiJson<{
          detected_model?: {
            workdir?: unknown;
            variants?: unknown;
            checked_at?: unknown;
          } | null;
        }>("GET", `/api/chat/conversations/${encodeURIComponent(convId)}`);
        const raw = summary.detected_model;
        let detectedModel: ChatTab["detectedModel"] = null;
        if (raw !== null && raw !== undefined && typeof raw === "object") {
          const workdir = typeof raw.workdir === "string" ? raw.workdir : "";
          const variants: { precision: string; label: string }[] = [];
          if (Array.isArray(raw.variants)) {
            for (const v of raw.variants) {
              if (v === null || typeof v !== "object") continue;
              const precision = (v as Record<string, unknown>).precision;
              const label = (v as Record<string, unknown>).label;
              if (typeof precision === "string" && typeof label === "string") {
                variants.push({ precision, label });
              }
            }
          }
          const checkedAt =
            typeof raw.checked_at === "string" ? raw.checked_at : undefined;
          detectedModel = {
            workdir,
            variants,
            ...(checkedAt !== undefined ? { checkedAt } : {}),
          };
        }
        // Only patch if the tab still exists AND still points at the same
        // conversation (guard against a mid-flight tab/conversation switch).
        const still = this.tabs.find((t) => t.id === tabId);
        if (still !== undefined && still.conversationId === convId) {
          this._patchTab(tabId, { detectedModel });
        }
      } catch {
        // Best-effort — a refresh hiccup must never disturb turn completion.
      }
    },

    /**
     * User asked to cancel mid-stream. Transitions streaming → aborting.
     * Transport layer is responsible for actually triggering the WS
     * `stop` / aborting the fetch. Aborting → idle is via
     * `confirmAbort()`.
     */
    requestCancel(tabId: TabId): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      if (tab.status !== "streaming") {
        return;
      }
      // V1 PARITY (useChat.js:2779-2796): a user-pressed Stop does NOT discard
      // the pending queue. V1 only clears the queue when the abort was caused
      // by SWITCHING CONVERSATIONS (`wasAbortedByNavigation`, useChat.js:2781-2785)
      // — those queued messages were meant for the conversation the user just
      // left, so re-sending them on a different conversation would be wrong. A
      // plain Stop (`activeConvId === sendingConvId`) falls through to the
      // dequeue tail (useChat.js:2788-2793) and KEEPS processing the queue:
      // the interrupted turn ends, then the next queued message is sent.
      //
      // V2 is per-tab: each tab owns its own queue bound to its conversation,
      // and switching tabs/conversations leaves the background turn streaming
      // (it never calls requestCancel — V1 background parity). So a
      // requestCancel here is ALWAYS the "plain Stop" case, which must PRESERVE
      // the queue (the prior code wrongly cleared it unconditionally, so a Stop
      // dropped every pending message and nothing was ever processed —
      // "队列中所有内容消失，但问题没有被正常处理"). The aborting→idle transition
      // (confirmAbort) is picked up by ChatView's dequeue watcher, which then
      // re-sends the next queued item (mirroring V1's recursive sendMessage).
      //
      // We still dismiss any blocking question dialog (the server-side handler
      // observes the abort and unwinds the suspended tool — V2 enhancement).
      //
      // SUBAGENT-STOP-CASCADE (fix A): a user Stop on a MAIN / take-over tab
      // that has spawned sub-agents which are STILL RUNNING must ALSO stop
      // those sub-agents — the user's mental model is "I stopped this agent,
      // its helpers should stop too". Historically stopping the parent turn
      // merely stopped the parent's CONSUMPTION of the sub-agent iterator
      // (`agent_tool.py` fan-out is driven by the parent `async for`), leaving
      // the sub-agent registered in `subagent_abort_registry` and — from the
      // UI's perspective — a stale `running`/`streaming` state that later
      // diverged from reality (the reported "主停了、子还在跑、去子标签停按钮
      // 不变" bug). We collect every still-running sub-agent id under THIS
      // tab's root conversation — the WHOLE tree at any depth (direct children
      // via inline blocks + grand/great-grand descendants via `subAgentIndex`),
      // so a grand-child is not orphaned one level down — and fire
      // `interruptSubAgent` for each. This MUST run BEFORE
      // `confirmAbort` → `buildConfirmAbortMessages` rewrites the running
      // sub-agent blocks to `error/interrupted` (messageCommit.ts:139-144),
      // which would otherwise make them un-findable here. Best-effort: a failed
      // child interrupt POST never blocks the parent Stop (interruptSubAgent
      // swallows its own errors + rolls back only its own optimistic marks).
      for (const sid of this._collectRunningChildSubAgentIds(tab)) {
        void this.interruptSubAgent(sid);
      }
      this._patchTab(tabId, {
        status: "aborting",
        pendingQuestion: null,
      });
    },

    /**
     * Collect the ids of every sub-agent under `tab`'s root conversation that
     * is still running, so a parent Stop can cascade-interrupt the WHOLE tree
     * (see `requestCancel`) — not just the first level. Two truth-sources are
     * merged (deduped via a shared `Set`):
     *
     *   ① INLINE BLOCKS (immediate, depth-1) — read from the LIVE UI state
     *      that exists BEFORE the abort settles the blocks:
     *        · committed round messages' `subAgentBlocks[]` with
     *          `status === "running"` (the common inline shape while the parent
     *          turn streams),
     *        · the in-flight `tab.streamingSubAgentBlocks[]` with
     *          `status === "running"` (the current round's not-yet-committed
     *          blocks).
     *      These capture the freshest state of the DIRECT children and also
     *      cover not-yet-persisted / index-not-loaded spawns.
     *
     *   ② WHOLE-TREE (any depth, incl. grand / great-grand) — read from
     *      `subAgentIndex[tab.conversationId]`, the per-root-conversation cache
     *      of EVERY sub-agent ever spawned under this main conversation at ANY
     *      depth (the SubAgentRail's data source). Every entry whose last-known
     *      `status === "running"` is collected regardless of `depth`, so a
     *      grand-child that would otherwise be orphaned (its parent sub-agent's
     *      turn stops consuming its iterator, leaving it registered + running on
     *      the backend — the same divergence as the original bug, one level
     *      down) is ALSO interrupted at the source.
     *
     * `tab.conversationId` may be `null` (a brand-new, not-yet-persisted
     * conversation) — then there is no index key and we fall back to the inline
     * block scan alone (acceptable: an unpersisted sub-agent is still present as
     * an inline block on `tab`).
     *
     * Dedupes by id; skips entries/blocks lacking a persistent `subagent_id`
     * (a legacy / not-yet-persisted sub-agent has nothing to POST an interrupt
     * to). Re-interrupting an id that appears in BOTH sources — or that the user
     * also stops manually — issues only ONE POST thanks to `interruptSubAgent`'s
     * `_inflightInterrupts` coalescing. Pure read — does not mutate.
     */
    _collectRunningChildSubAgentIds(tab: ChatTab): string[] {
      const ids = new Set<string>();
      // ① Inline blocks (depth-1, freshest immediate truth).
      const collect = (blocks: SubAgentBlock[] | undefined): void => {
        if (blocks === undefined) return;
        for (const b of blocks) {
          if (b === undefined) continue;
          if (b.status !== "running") continue;
          const sid = b.subagent_id;
          if (typeof sid === "string" && sid !== "") ids.add(sid);
        }
      };
      for (const msg of tab.messages) collect(msg.subAgentBlocks);
      collect(tab.streamingSubAgentBlocks);
      // ② Whole-tree from the per-root-conversation index (any depth). Skips
      //    when the tab has no persisted conversation id (no index key).
      const convId = tab.conversationId;
      if (convId !== null && convId !== "") {
        const entries = this.subAgentIndex[convId] ?? [];
        for (const e of entries) {
          if (e.status !== "running") continue;
          const sid = e.subagentId;
          if (typeof sid === "string" && sid !== "") ids.add(sid);
        }
      }
      return [...ids];
    },

    confirmAbort(tabId: TabId): void {
      const found = this.tabs.find((t) => t.id === tabId);
      if (found === undefined) {
        return;
      }
      // Allowed from aborting OR streaming (defensive: server may have
      // closed during the aborting handshake before we observed it).
      if (found.status !== "aborting" && found.status !== "streaming") {
        return;
      }
      // Flush rAF-buffered text so the interrupted message keeps everything
      // generated so far (V1 shows partial content on abort). Re-read after.
      cancelStreamingFlush(tabId);
      this._flushStreamingBuffer(tabId);
      cancelRoundChunkFlush(tabId);
      this._flushRoundChunkBuffer(tabId);
      const tab = this.tabs.find((t) => t.id === tabId) ?? found;
      // V1 useChat.js:2685-2712 + chat_handler.py:704 parity — always
      // commit an assistant message tagged `meta.interrupted=true` on user
      // abort, even when no partial content arrived yet. V1's backend
      // injects "\n\n[操作已被用户中断]" as a stream delta before closing;
      // V2 renders the localized `chat.interruptedMark` at display-time
      // for any message with `meta.interrupted=true`, so the user sees
      // *what was generated so far* plus an inline "[Interrupted]" tail.
      // `alwaysCommit=true` ensures a message is committed even with zero
      // content (the render layer still shows the interrupted mark).
      const messages = buildConfirmAbortMessages(tab, nextMessageId(), true);
      this._patchTab(tabId, {
        status: "idle",
        streamingContent: "",
        streamingToolCalls: [],
        activeToolMessageId: null,
        roundMessageIds: {},
        roundSubAgentMessageIds: {},
        activeSubAgentMessageId: null,
        streamingUsage: null,
        streamingPerf: null,
        streamingSubAgentBlocks: [],
        streamingRequestId: null,
        compacting: false,
        messages,
        networkRetry: null,
        streamingSenderId: null,
        streamingSenderName: null,
        streamingSenderColor: null,
        streamingSenderModelId: null,
      });
    },

    /**
     * Record a server error frame: streaming → error.
     *
     * V1 parity (useChat.js:2685-2745): preserve any partial output and
     * residual running tool cards by committing them as an interrupted
     * assistant message (same shape as `confirmAbort`), instead of
     * silently dropping `streamingContent` and leaving tool bubbles
     * stuck `running` until the user starts a new turn (State-Truth-First
     * fix: a turn that ended in error must still settle the live UI to
     * a terminal state). The error envelope itself stays on `lastError`
     * so the error banner / retry path still works.
     */
    recordError(
      tabId: TabId,
      err:
        | ApiError
        | {
            type: string;
            code: string;
            message: string;
            retryDisposition?: string | null;
            httpStatus?: number | null;
            requestId?: string | null;
          },
    ): void {
      const found = this.tabs.find((t) => t.id === tabId);
      if (found === undefined) {
        return;
      }
      // Flush rAF-buffered text so the interrupted message keeps partial
      // output (same contract as confirmAbort). Re-read the tab afterwards.
      cancelStreamingFlush(tabId);
      this._flushStreamingBuffer(tabId);
      cancelRoundChunkFlush(tabId);
      this._flushRoundChunkBuffer(tabId);
      const tab = this.tabs.find((t) => t.id === tabId) ?? found;
      const messages = buildConfirmAbortMessages(tab, nextMessageId());
      this._patchTab(tabId, {
        status: "error",
        lastError: {
          type: err.type,
          code: err.code,
          message: err.message,
          // Optional diagnostic fields (present only on LLM stream ERROR
          // frames threaded through the transport). `ApiError` (the HTTP
          // envelope) has none of these, so read defensively.
          retryDisposition:
            "retryDisposition" in err ? (err.retryDisposition ?? null) : null,
          httpStatus: "httpStatus" in err ? (err.httpStatus ?? null) : null,
          requestId: "requestId" in err ? (err.requestId ?? null) : null,
        },
        messages,
        streamingContent: "",
        streamingToolCalls: [],
        activeToolMessageId: null,
        roundMessageIds: {},
        roundSubAgentMessageIds: {},
        activeSubAgentMessageId: null,
        streamingUsage: null,
        streamingPerf: null,
        streamingSubAgentBlocks: [],
        streamingRequestId: null,
        compacting: false,
        streamingSenderId: null,
        streamingSenderName: null,
        streamingSenderColor: null,
        streamingSenderModelId: null,
      });
    },

    /** Clear the error state and return to idle. */
    resetError(tabId: TabId): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      if (tab.status !== "error") {
        return;
      }
      this._patchTab(tabId, { status: "idle", lastError: null, networkRetry: null });
    },

    // ---------------------------------------------------------------
    // Message helpers
    // ---------------------------------------------------------------

    /**
     * Append a user message and transition idle → streaming.
     * Returns the message id; transport layer should then call
     * `setStreaming` is implicit here (this method handles the
     * transition atomically with appending the user prompt).
     */
    pushUserMessage(tabId: TabId, prompt: string): string | null {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return null;
      }
      if (tab.status !== "idle") {
        return null;
      }
      const id = nextMessageId();
      const msg: ChatMessage = {
        id,
        role: "user",
        content: prompt,
        createdAt: Date.now(),
        ...(tab.conversationId !== null
          ? { conversationId: tab.conversationId }
          : {}),
      };
      this._patchTab(tabId, {
        status: "streaming",
        messages: [...tab.messages, msg],
        streamingContent: "",
        lastError: null,
        lastActiveAt: Date.now(),
      });
      return id;
    },

    appendMessage(tabId: TabId, message: ChatMessage): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      this._patchTab(tabId, { messages: [...tab.messages, message] });
    },

    /**
     * Open a sub-agent's conversation in a NEW tab so the user can inspect
     * its transcript and take over the conversation (V2 enhancement; the
     * agent capability lives inside the ordinary `qai.chat` flow).
     *
     * Fetches the full sub-agent detail (`GET /api/chat/subagents/{id}`),
     * opens a `kind: "subagent"` tab bound to the parent conversation, maps
     * the returned `messages` (role ∈ user/assistant/tool — already restored
     * to a renderable list by the backend) into `ChatMessage[]`, and switches
     * to the tab. When the user later sends a message in this tab the
     * transport forwards `subagent_id` (read off `tab.subagentMeta`) so the
     * backend continues the turn on the sub-agent's context.
     *
     * Reuses an existing sub-agent tab if one is already open for this id
     * (mirrors `selectConversation`'s tab-reuse). Returns the tab id on
     * success, or `null` when a concurrent open for the same id is already in
     * flight (rapid-click guard). THROWS if loading the sub-agent transcript
     * fails (so the caller can surface a visible error toast — a silent dead
     * click was the "Open in new tab does nothing" bug).
     */
    async openSubAgentTab(subagentId: string): Promise<TabId | null> {
      // Reuse an already-open sub-agent tab for the same id.
      const existing = this.tabs.find(
        (t) => t.kind === "subagent" && t.subagentMeta?.subagentId === subagentId,
      );
      if (existing !== undefined) {
        this.switchTab(existing.id);
        // Hydrate-on-reuse (Crit-rail-empty fix): when the legacy sub-agent
        // tab carries an empty messages array (e.g. a `restoreLayout`
        // placeholder whose `_hydrateRestoredSubAgentTab` failed/raced, or
        // a tab created by an out-of-band path that never fetched detail),
        // the WS subscription below CANNOT backfill the transcript — the
        // broadcaster's `_replay_without_entry` fallback (used once the
        // in-memory buffer TTL expires, i.e. for any sub-agent already
        // `done`) only emits `state` + `done` frames and explicitly delegates
        // history materialisation to "the snapshot REST endpoint the client
        // already fetched before subscribing" (see
        // `sub_agent_stream_broadcaster.py:296`). For a placeholder that
        // never fetched, that assumption is false → `messages` stays at 0
        // → `showWelcome` fires → user sees the welcome screen instead of
        // the sub-agent's transcript.
        //
        // Three sub-agent states all flow through here:
        //   - DONE (most common — historical conversations, completed runs):
        //     broadcaster's buffer is TTL-expired so WS replay is just
        //     state+done; HTTP GET returns the full persisted transcript.
        //   - RUNNING: broadcaster buffer is live; HTTP GET returns the
        //     transcript-so-far, then WS resumes from cursor 0 (cursor-0
        //     replay is idempotent — duplicates dedup'd by frame_id).
        //   - ABORTED/ERROR: same as DONE — terminal status persisted.
        //
        // Concurrency guard: same `_openingSubAgentIds` set as new-open
        // path → rapid double-click on the same chip is a no-op while the
        // first GET is in flight.
        if (
          existing.messages.length === 0 &&
          !_openingSubAgentIds.has(subagentId)
        ) {
          _openingSubAgentIds.add(subagentId);
          try {
            await this._refreshSubAgentTab(existing.id, subagentId);
          } catch (err) {
            console.error("[chatTabs] reuse-hydrate _refreshSubAgentTab threw", err);
          } finally {
            _openingSubAgentIds.delete(subagentId);
          }
        }
        // Re-subscribe + refresh on reuse: the tab may have been opened earlier
        // and its live stream already torn down (terminal close) OR it never
        // subscribed; without this a "re-open" of an existing tab would show
        // STALE content (the reported "切走切回/重开才更新" symptom). Aborting
        // any prior controller first is handled inside `_subscribeSubAgentStream`
        // via `attachSubAgentStream`. The cursor-0 replay backfills anything
        // missed; if the sub-agent is terminal it replays the full buffer then
        // closes (→ `refreshFromSnapshot`), so reuse always lands on fresh data.
        this._subscribeSubAgentStream(existing.id, subagentId);
        return existing.id;
      }
      // Guard: if a previous click already started the async open flow for this
      // sub-agent (network fetch in flight, tab not yet created), bail out to
      // prevent duplicate tabs from rapid clicks.
      if (_openingSubAgentIds.has(subagentId)) {
        return null;
      }
      _openingSubAgentIds.add(subagentId);
      try {
        return await this._openSubAgentTabInner(subagentId);
      } finally {
        _openingSubAgentIds.delete(subagentId);
      }
    },

    /** Inner implementation of openSubAgentTab, called only once per
     *  sub-agent id at a time (guarded by `_openingSubAgentIds`). */
    async _openSubAgentTabInner(subagentId: string): Promise<TabId | null> {
      const { apiJson } = await import("@/api");
      interface SubAgentMessageItem {
        role: string;
        text?: string | null;
        tool_calls?: ChatToolCall[] | null;
        tool_call_id?: string | null;
        name?: string | null;
        created_at?: string | null;
        // 回看 parity: an assistant turn carries that round's token usage +
        // meta.request_id (per-message token line + 📄 snapshot button), shaped
        // like the main-agent `MessageItem`, so the SHARED `mapHistoryItems`
        // renders an IDENTICAL per-message badge + button.
        usage?: ChatMessageUsage | null;
        meta?: Record<string, unknown> | null;
      }
      interface SubAgentDetail {
        subagent_id: string;
        status: string;
        owner: string;
        subagent_type?: string;
        title?: string | null;
        prompt_preview?: string | null;
        rounds?: number;
        root_conversation_id: string;
        parent_subagent_id?: string | null;
        depth?: number;
        created_at?: string;
        updated_at?: string;
        messages?: SubAgentMessageItem[];
        // Sub-agent's OWN context usage (badge fix). Estimated server-side
        // from the sub-agent's persisted wire history; the context badge
        // reads these instead of querying the PARENT conversation's /context.
        used_tokens?: number;
        budget_tokens?: number;
        ratio?: number;
        // Appended (§3.1): the REAL (un-clamped) occupancy, same口径 as the
        // main agent's /context badge. `used_tokens`/`ratio` above are clamped
        // to the window (floor 100%); these preserve the over-window truth so
        // the sub-agent badge can show >100% at parity with the main agent.
        raw_used_tokens?: number;
        raw_ratio?: number;
        // Sub-agent's OWN model (§3.1 tail-appended): session-persisted
        // model_id (= spawning parent's model at spawn; updated via PATCH when
        // the user switches it on this sub-agent tab) + its provider slug
        // (may be null until the first switch backfills it). The backend now
        // resolves the budget from session.model_id, so this is the
        // authoritative model for both the ModelDropdown display and the
        // context-badge denominator (State-Truth-First).
        model_id?: string | null;
        model_provider?: string | null;
        // Whether the main agent granted this sub-agent the ability to spawn
        // its own sub-agents (migration 045). Used to DEFAULT the take-over
        // tab's `selfAllowSpawn` toggle ON (user may still turn it off).
        allow_spawn?: boolean;
      }
      // Budget口径 (bug ② fix): the sub-agent's context-window denominator
      // must reflect the SUB-AGENT's OWN model, not the parent badge's window.
      // A sub-agent runs with the model of the tab that SPAWNED it
      // (`agent_tool.py`: `model_hint` = the parent turn's model). The
      // `SubAgentSession` does NOT persist that model, so for this COLD-OPEN GET
      // we use the active tab's model as a best-effort first guess (opening a
      // sub-agent almost always happens from the conversation that spawned it,
      // i.e. the active tab IS the spawning parent). The AUTHORITATIVE running
      // value is the LIVE stream's `context_limit` (resolved from the
      // sub-agent's real `model_hint` in `agent_tool.py`), which overrides this
      // estimate the moment the first live frame arrives — so a transient
      // mismatch (user switched to an unrelated tab before opening) self-heals.
      // (treat the "qai-default" placeholder as "no model" → backend default).
      const parentModelId = this.activeTab?.modelId ?? null;
      const detailQuery =
        parentModelId !== null &&
        parentModelId !== "" &&
        parentModelId !== "qai-default"
          ? `?model_id=${encodeURIComponent(parentModelId)}`
          : "";
      let detail: SubAgentDetail;
      try {
        detail = await apiJson<SubAgentDetail>(
          "GET",
          `/api/chat/subagents/${encodeURIComponent(subagentId)}${detailQuery}`,
        );
      } catch (err) {
        // State-Truth-First: do NOT swallow the failure silently — that made
        // "Open in new tab" appear to do nothing (the classic symptom when the
        // sub-agent session isn't persisted yet / the GET 404s or 500s). Surface
        // the real cause in the console for diagnosis, and re-throw so the
        // caller (ChatMessageList.handleOpenSubAgent) shows the user a visible
        // toast instead of a dead click. Note: the rapid-click guard in
        // `openSubAgentTab` returns null WITHOUT throwing, so a benign
        // already-opening click never triggers an error toast.
        console.error(
          `[chatTabs] openSubAgentTab: failed to load sub-agent ${subagentId}`,
          err,
        );
        throw err;
      }
      const label =
        detail.title?.trim() ||
        detail.prompt_preview?.trim() ||
        subagentId;
      // Save the current active tab so we don't flash an empty sub-agent tab
      // to the user while messages are being built. `openTab` auto-activates
      // the new tab (line ~484); restore the prior focus and only switch once
      // the messages + SSE subscription are ready.
      const prevActiveTabId = this.activeTabId;
      // Sub-agent's OWN model is the AUTHORITATIVE session value
      // (State-Truth-First): seed the tab's modelId/modelProvider from the
      // detail so the ModelDropdown shows THIS sub-agent's model (not the
      // global auto-seed default — fixes the D11 split), and mirror it onto
      // `subagentMeta.modelId/modelProvider`. Treat a null/empty provider as
      // "" (the auto-seed watcher / a later PATCH backfills it).
      const saModelId =
        typeof detail.model_id === "string" && detail.model_id !== ""
          ? detail.model_id
          : null;
      const saModelProvider =
        typeof detail.model_provider === "string" ? detail.model_provider : "";
      const tab = this.openTab({
        title: `SubAgent: ${label}`,
        conversationId: detail.root_conversation_id,
        kind: "subagent",
        ...(saModelId !== null
          ? { modelId: saModelId, modelProvider: saModelProvider }
          : {}),
        subagentMeta: {
          subagentId: detail.subagent_id,
          rootConversationId: detail.root_conversation_id,
          parentSubagentId: detail.parent_subagent_id ?? null,
          depth: detail.depth ?? 1,
          status: detail.status,
          owner: detail.owner,
          ...(typeof detail.used_tokens === "number"
            ? { usedTokens: detail.used_tokens }
            : {}),
          ...(typeof detail.budget_tokens === "number"
            ? { budgetTokens: detail.budget_tokens }
            : {}),
          ...(typeof detail.ratio === "number" ? { ratio: detail.ratio } : {}),
          ...(typeof detail.raw_used_tokens === "number"
            ? { rawUsedTokens: detail.raw_used_tokens }
            : {}),
          ...(typeof detail.raw_ratio === "number"
            ? { rawRatio: detail.raw_ratio }
            : {}),
          ...(saModelId !== null
            ? { modelId: saModelId, modelProvider: saModelProvider }
            : {}),
        },
      });
      // Spawn-permission default (migration 045): when the main agent granted
      // this sub-agent the ability to create its own sub-agents, DEFAULT the
      // take-over tab's `selfAllowSpawn` toggle ON so the "allow this sub-agent
      // to create sub-agents" button is lit on open (the user may still turn
      // it off — it is a default, not a lock). When not granted, leave it
      // undefined (off) so the historical behaviour is unchanged.
      if (detail.allow_spawn === true) {
        this.setSelfAllowSpawn(tab.id, true);
      }
      // Restore focus to the prior tab until messages are ready — prevents the
      // user from seeing a blank welcome/skeleton on the freshly-created tab.
      if (prevActiveTabId !== null) {
        this.activeTabId = prevActiveTabId;
      }
      // Map the restored sub-agent transcript into ChatMessage[] using the
      // SAME mapper the main agent uses for its persisted history
      // (`mapHistoryItems`). Full-unification: the backend now serialises the
      // sub-agent's AUTHORITATIVE structured transcript
      // (`SubAgentSession.messages` — built once at persist time, the SAME
      // `Message` shape the main agent stores) DIRECTLY into `detail.messages`
      // (no `_wire_to_messages` reverse-fold); each assistant turn's
      // `tool_calls` already carry the executed `output` paired by
      // `tool_call_id` + the Vertex `thought_signature`, and standalone
      // `role:tool` turns are not emitted — so one mapper covers both agents,
      // zero divergence. A legacy (pre-structured) row still round-trips via the
      // backend's wire fallback in the SAME shape, so this path is unchanged.
      // Base fallback timestamp on the sub-agent's CREATION time (not its
      // last-update time) so a turn lacking its own `created_at` (only legacy
      // wire rows) pins to the correct start instead of `updated_at`. Structured
      // messages now carry a real per-turn `created_at`, so the `+ i` synthesis
      // below is only a safety net for legacy rows.
      const baseIso = detail.created_at ?? detail.updated_at;
      const baseTs = baseIso ? Date.parse(baseIso) : Date.now();
      const createdMs = Number.isFinite(baseTs) ? baseTs : Date.now();
      const historyItems = (detail.messages ?? []).map((m, i) => ({
        id: `${subagentId}:${i}`,
        role: m.role,
        text: typeof m.text === "string" ? m.text : "",
        // Prefer the REAL per-turn timestamp (structured messages always carry
        // it); fall back to `created_at + i` only for a legacy wire-derived row
        // that predates per-turn timestamps.
        created_at:
          typeof m.created_at === "string" && m.created_at
            ? m.created_at
            : new Date(createdMs + i).toISOString(),
        parent_id: null,
        ...(m.tool_calls && m.tool_calls.length > 0
          ? { tool_calls: m.tool_calls }
          : {}),
        // 回看 parity: surface per-round request_id + usage (stamped by the
        // backend onto the assistant turn) so the SHARED `mapHistoryItems`
        // renders the per-message 📄 button + token line IDENTICAL to the main
        // agent — no sub-agent-specific snapshot/usage UI.
        ...(m.usage != null ? { usage: m.usage } : {}),
        ...(m.meta != null ? { meta: m.meta } : {}),
      }));
      const messages: ChatMessage[] = mapHistoryItems(
        historyItems,
        detail.root_conversation_id,
      );
      // Rehydrate the top TaskListBar from the newest `todowrite` call in the
      // sub-agent's persisted transcript — identical to the main-agent history
      // path (`loadHistoryMessages`, see `todoList: extractLatestTodoList(...)`).
      // The bar reads `tab.todoList`, which is otherwise only filled live; on a
      // reopened (terminal) sub-agent tab the live stream is skipped, so without
      // this the task-list pill would stay hidden even though the sub-agent's
      // todowrite snapshot cards still render in the transcript. The sub-agent's
      // returned `tool_calls[]` use the SAME `tool` field as the main agent, so
      // the shared extractor finds the todos. Empty when no todowrite call.
      this._patchTab(tab.id, {
        messages,
        todoList: extractLatestTodoList(messages),
      });
      // SubAgentRail index cache — mirror the freshly-fetched detail into the
      // per-root-conversation index so the rail shows this sub-agent even
      // after the user closes its tab (chip stays greyed / re-hydratable).
      // See `SubAgentIndexEntry` docstring for the full lifecycle.
      this._upsertSubAgentIndexEntry({
        subagentId: detail.subagent_id,
        rootConversationId: detail.root_conversation_id,
        parentSubagentId: detail.parent_subagent_id ?? null,
        depth: detail.depth ?? 1,
        title: label,
        status: detail.status,
        owner: detail.owner,
        ...(typeof detail.used_tokens === "number"
          ? { usedTokens: detail.used_tokens }
          : {}),
        ...(typeof detail.budget_tokens === "number"
          ? { budgetTokens: detail.budget_tokens }
          : {}),
        ...(saModelId !== null
          ? { modelId: saModelId, modelProvider: saModelProvider }
          : {}),
      });
      this.switchTab(tab.id);
      // Block 2 — subscribe to the sub-agent's LIVE event stream (WS) so the
      // tab updates in real time AND, on a close+reopen while it is still
      // running, backfills the frames missed in between (the broadcaster
      // replays its buffer from cursor 0). The subscription is fire-and-forget;
      // tab close aborts it (see closeTab → clearSubAgentStream).
      //
      // ALWAYS subscribe — do NOT gate on the snapshot `detail.status`.
      // State-Truth-First (AGENTS.md §7): the one-shot `status` fetched on open
      // is a snapshot that can RACE the real run — a sub-agent that is actually
      // still producing output can report `done` for the instant we opened (or
      // the persisted status lags the in-memory broadcaster), and gating on it
      // meant we skipped the live subscription and the tab then never showed
      // the rest of the run (the reported "子 Agent 标签页不实时更新" bug).
      // The broadcaster decides terminality, not this snapshot: subscribing to
      // an already-finished sub-agent simply replays its full buffer (or the
      // persisted snapshot) then closes — cheap and race-free.
      this._subscribeSubAgentStream(tab.id, detail.subagent_id);
      // The "open in new tab" button appears at `subagent_start`, so the user
      // can open the tab BEFORE round 1 persists the sub-agent's wire history +
      // token usage. In that case the initial GET returns `messages: []` and
      // `used_tokens: 0`, so the prompt is missing and the context badge shows
      // 0 — and nothing re-fetches while the tab stays open (the only auto
      // refresh is the live stream's TERMINAL `refreshFromSnapshot`). That is
      // exactly why a manual close+reopen "fixed" it: reopen forces a fresh GET
      // after the data exists. Schedule a few bounded, self-cancelling
      // corrective re-fetches that do the same automatically.
      this._scheduleSubAgentBackfill(tab.id, detail.subagent_id);
      return tab.id;
    },

    /**
     * Corrective backfill for a sub-agent tab opened "too fast" (right at
     * `subagent_start`, before round 1 persisted its wire + token usage).
     *
     * Re-fetches the persisted snapshot via {@link _refreshSubAgentTab} on a
     * short, bounded backoff until the detail is populated (`messages` present
     * AND `usedTokens > 0`), the tab reaches a terminal status (the live
     * stream's `refreshFromSnapshot` then owns the final transcript), or the
     * tab is closed. Self-cancelling: it stops the moment any of those
     * conditions holds and is hard-bounded by the fixed `delays` list, so it
     * can never loop indefinitely or hammer the endpoint.
     */
    _scheduleSubAgentBackfill(tabId: TabId, subagentId: string): void {
      const delays = [500, 1200, 2500, 5000];
      let i = 0;
      const tick = async (): Promise<void> => {
        const tab = this.tabs.find((t) => t.id === tabId);
        // Tab closed / gone, or no longer a sub-agent tab → stop.
        if (tab === undefined || tab.kind !== "subagent") return;
        const meta = tab.subagentMeta;
        const hasMsgs = tab.messages.length > 0;
        const hasTokens = (meta?.usedTokens ?? 0) > 0;
        // Populated → nothing more to backfill.
        if (hasMsgs && hasTokens) return;
        // Terminal: the live stream's terminal `refreshFromSnapshot` already
        // owns the authoritative final transcript; don't double-fetch.
        if (meta?.status !== undefined && meta.status !== "running") return;
        // Re-fetch the persisted snapshot (same GET a manual reopen would do).
        await this._refreshSubAgentTab(tabId, subagentId);
        const d = delays[i];
        if (d !== undefined) {
          i += 1;
          setTimeout(() => {
            void tick();
          }, d);
        }
      };
      // Kick off the first attempt after the shortest delay (round 1 typically
      // persists within the first second or two).
      setTimeout(() => {
        void tick();
      }, delays[0]);
    },

    /**
     * Populate `state.subAgentIndex[rootConvId]` from the backend's
     * `GET /api/chat/conversations/{convId}/subagents` list, so the
     * SubAgentRail can show EVERY sub-agent ever spawned under this main
     * conversation — including ones whose tab isn't currently open.
     *
     * Called by the main-tab history-load path (`loadHistoryMessages`) and by
     * `switchTab` (as an opportunistic refresh when the user switches INTO a
     * main tab whose index is still empty). Idempotent: silently returns when
     * `rootConvId` already has a non-empty cached list. Non-fatal on error —
     * the rail then falls back to filtering `state.tabs` (graceful
     * degradation).
     *
     * State-Truth-First: the backend's `SubAgentSession` table is the truth.
     * This cache mirrors it read-only + is kept incrementally in sync by
     * `_upsertSubAgentIndexEntry` on every open/refresh/spawn event.
     */
    async _fetchSubAgentIndex(rootConvId: ConversationId): Promise<void> {
      if (rootConvId === "" || rootConvId === null) return;
      // Short-circuit when we've already populated this conversation's list
      // (later incremental updates keep it fresh).
      const existing = this.subAgentIndex[rootConvId] ?? [];
      if (existing.length > 0) return;
      const { apiJson } = await import("@/api");
      try {
        interface SubAgentSummaryItem {
          subagent_id: string;
          root_conversation_id: string;
          parent_subagent_id?: string | null;
          depth?: number;
          title?: string | null;
          prompt_preview?: string | null;
          status: string;
          owner: string;
          used_tokens?: number;
          budget_tokens?: number;
          model_id?: string | null;
          model_provider?: string | null;
        }
        interface SubAgentListResp {
          items?: SubAgentSummaryItem[];
        }
        const resp = await apiJson<SubAgentListResp>(
          "GET",
          `/api/chat/conversations/${encodeURIComponent(rootConvId)}/subagents`,
        );
        const items = resp.items ?? [];
        if (items.length === 0) {
          // Explicitly write an empty array so a later fetch short-circuits
          // (rather than repeatedly round-tripping to confirm still-empty).
          this.subAgentIndex = {
            ...this.subAgentIndex,
            [rootConvId]: [],
          };
          return;
        }
        const entries: SubAgentIndexEntry[] = items.map((it) => {
          const rawTitle =
            (typeof it.title === "string" ? it.title : "").trim() ||
            (typeof it.prompt_preview === "string"
              ? it.prompt_preview
              : ""
            ).trim() ||
            it.subagent_id;
          const entry: SubAgentIndexEntry = {
            subagentId: it.subagent_id,
            rootConversationId: it.root_conversation_id,
            parentSubagentId: it.parent_subagent_id ?? null,
            depth: it.depth ?? 1,
            title: rawTitle,
            status: it.status,
            owner: it.owner,
            ...(typeof it.used_tokens === "number"
              ? { usedTokens: it.used_tokens }
              : {}),
            ...(typeof it.budget_tokens === "number"
              ? { budgetTokens: it.budget_tokens }
              : {}),
            ...(typeof it.model_id === "string" && it.model_id !== ""
              ? {
                  modelId: it.model_id,
                  modelProvider:
                    typeof it.model_provider === "string"
                      ? it.model_provider
                      : "",
                }
              : {}),
          };
          return entry;
        });
        this.subAgentIndex = {
          ...this.subAgentIndex,
          [rootConvId]: entries,
        };
      } catch {
        // Non-fatal — the rail falls back to filtering `state.tabs`.
      }
    },

    /**
     * Upsert one sub-agent's index entry (merge by `subagentId`). Called on
     * every open / refresh / spawn event so the rail stays fresh WITHOUT
     * re-fetching the whole list. Missing rootConvId falls through the guard.
     */
    _upsertSubAgentIndexEntry(entry: SubAgentIndexEntry): void {
      const rootConvId = entry.rootConversationId;
      if (rootConvId === "") return;
      const cur = this.subAgentIndex[rootConvId] ?? [];
      const existingIdx = cur.findIndex((e) => e.subagentId === entry.subagentId);
      let next: SubAgentIndexEntry[];
      if (existingIdx < 0) {
        next = [...cur, entry];
      } else {
        // Merge — preserve existing fields when the incoming value is
        // undefined (don't erase a populated `usedTokens` etc. on a light
        // status-only update).
        const prev = cur[existingIdx]!;
        const merged: SubAgentIndexEntry = { ...prev };
        const r = entry as unknown as Record<string, unknown>;
        const m = merged as unknown as Record<string, unknown>;
        for (const key of Object.keys(r)) {
          const v = r[key];
          if (v !== undefined) {
            m[key] = v;
          }
        }
        next = [
          ...cur.slice(0, existingIdx),
          merged,
          ...cur.slice(existingIdx + 1),
        ];
      }
      this.subAgentIndex = {
        ...this.subAgentIndex,
        [rootConvId]: next,
      };
    },

    /**
     * Update just the `status` of one sub-agent index entry. Cheap fast-path
     * for live status transitions (`running` / `done` / `error` / `aborting`)
     * that don't need a full detail re-fetch. No-op when the entry doesn't
     * exist yet — a later `_upsertSubAgentIndexEntry` or `_fetchSubAgentIndex`
     * will create it.
     */
    _updateSubAgentIndexStatus(subagentId: string, status: string): void {
      for (const [convId, entries] of Object.entries(this.subAgentIndex)) {
        const idx = entries.findIndex((e) => e.subagentId === subagentId);
        if (idx < 0) continue;
        const prev = entries[idx]!;
        if (prev.status === status) return;
        const merged: SubAgentIndexEntry = { ...prev, status };
        const next = [
          ...entries.slice(0, idx),
          merged,
          ...entries.slice(idx + 1),
        ];
        this.subAgentIndex = {
          ...this.subAgentIndex,
          [convId]: next,
        };
        return;
      }
    },

    /**
     * Interrupt a RUNNING sub-agent by its id (block 3).
     *
     * Calls `POST /api/chat/subagents/{id}/interrupt`, which signals the
     * sub-agent's INDEPENDENT cancellation flag so ONLY that sub-agent stops
     * (cooperatively, after its current round) — the parent tab / main agent
     * are untouched. Best-effort: returns `true` when the backend reports the
     * sub-agent was actually in-flight and signalled (`aborted`), `false`
     * otherwise (already finished / not found / network error). When the
     * sub-agent has a live tab open, its stream will close and refresh the
     * snapshot to the INTERRUPTED transcript.
     *
     * UX: BEFORE awaiting the POST, optimistically transition every UI surface
     * that surfaces this sub-agent into an `aborting` intermediate state so
     * the user sees IMMEDIATE feedback (the ⏹ button changes to a disabled
     * "stopping…" pill; standalone `kind === "subagent"` tabs flip
     * `streaming → aborting` which makes `useComposerSubmit.showStop` render
     * the spinner / aborting label). Backend abort latency is round-boundary
     * + tool-subprocess teardown (up to a few seconds on Windows), so without
     * this optimistic step the user sees no reaction and re-clicks. The
     * terminal `subagent_done` / `subagent_error` frame (or the snapshot
     * refresh on stream close) naturally settles the block back to
     * `done`/`error`, so `aborting` needs no explicit exit. If the backend
     * reports `aborted=false` (already finished / not found) OR the call
     * throws (network error) we ROLL BACK the optimistic state so the UI
     * doesn't lie. Re-entry is deduped — calling `interruptSubAgent` again
     * while already `aborting` is a no-op (returns `true`), preventing
     * duplicate POSTs from rapid double-click.
     *
     * Mirrors the main-agent `requestCancel` (chatTabs.ts:1946-1979) which
     * already does the equivalent `streaming → aborting` transition on the
     * parent tab; this brings sub-agents to parity.
     */
    /**
     * Settle every still-spinning INLINE `SubAgentBlock` for `subagentId` to a
     * terminal state, WITHOUT relying on a WS frame.
     *
     * The inline block lives inside a parent chat tab's
     * `message.subAgentBlocks[]`. When the user stops a sub-agent, that block
     * is flipped to `"aborting"` optimistically, but the parent tab has no
     * per-child WS subscription — so the terminal `subagent_done` /
     * `subagent_error` frame (which `frameHandlers` uses to clear the spinner)
     * never arrives on that tab and the card spins forever. This helper is the
     * WS-independent fallback: it walks ALL tabs and rebuilds any block whose
     * `subagent_id` matches and whose status is still non-terminal
     * (`running` / `aborting`) into `"done"` (mirrors
     * `handleSubagentDone`). Idempotent — blocks already `done` / `error` are
     * left untouched, so calling it after a real terminal frame is a no-op.
     */
    _settleInlineSubAgentBlocks(subagentId: string): void {
      if (subagentId === "") return;
      for (const tab of this.tabs) {
        // Inline blocks live on parent (chat) tabs; the standalone sub-agent
        // tab is settled separately via confirmAbort + snapshot refresh.
        if (tab.kind === "subagent") continue;
        for (let mi = 0; mi < tab.messages.length; mi++) {
          const msg = tab.messages[mi];
          const blocks = msg?.subAgentBlocks;
          if (blocks === undefined || blocks.length === 0) continue;
          let mutated = false;
          const nextBlocks = blocks.map((b) => {
            if (
              b.subagent_id === subagentId &&
              (b.status === "running" || b.status === "aborting")
            ) {
              mutated = true;
              return { ...b, status: "done" as const };
            }
            return b;
          });
          if (!mutated) continue;
          const nextMsg: ChatMessage = { ...msg!, subAgentBlocks: nextBlocks };
          const nextMessages = [
            ...tab.messages.slice(0, mi),
            nextMsg,
            ...tab.messages.slice(mi + 1),
          ];
          this._patchTab(tab.id, { messages: nextMessages });
        }
      }
    },

    async interruptSubAgent(subagentId: string): Promise<boolean> {
      // ── Concurrent-call coalescing ──────────────────────────────────────
      //
      // Multiple UI surfaces can fire `interruptSubAgent(sameId)` in quick
      // succession (rapid ⏹ clicks, parent-tab block ⏹ + ActiveRuns panel ⏹
      // hitting at the same time). The OLD dedupe path was "if everything is
      // already aborting, return true synchronously and skip the POST" — but
      // that has a subtle race: call A's POST may resolve `aborted=false`
      // (backend already settled naturally) and roll back the optimistic
      // marks; meanwhile call B had short-circuited to `true` without
      // registering its own rollback context, so the user sees the UI
      // briefly stuck "stopping…" then snap back to streaming, and B's
      // success return is a lie. Fix: a single in-flight POST per subagentId
      // — every concurrent caller SHARES the same promise and observes the
      // SAME final state (committed or rolled-back). Entry deleted in
      // `finally` so a fresh wake's interrupt starts cleanly.
      const existing = _inflightInterrupts.get(subagentId);
      if (existing !== undefined) {
        return existing;
      }
      const promise = (async (): Promise<boolean> => {
        return this._interruptSubAgentInner(subagentId);
      })();
      _inflightInterrupts.set(subagentId, promise);
      try {
        return await promise;
      } finally {
        _inflightInterrupts.delete(subagentId);
      }
    },

    /** Inner implementation of `interruptSubAgent`, called by the wrapper
     *  above (which adds in-flight-promise coalescing). Keep separated so
     *  the wrapper stays small and the body's logic is self-contained. */
    async _interruptSubAgentInner(subagentId: string): Promise<boolean> {
      // ── Collect every surface that references this sub-agent and is in a
      //    cancellable state ──────────────────────────────────────────────
      //
      // ① Standalone subagent-kind tabs whose subagentMeta.subagentId
      //    matches AND that are cancellable. Historically this only matched
      //    `tab.status === "streaming"`, but that is a fragile discriminator:
      //    a sub-agent tab can be genuinely running on the BACKEND yet have
      //    its local `tab.status` stuck at `idle` (never flipped to
      //    `streaming`) — e.g. the parent turn was stopped first, so the
      //    parent stopped CONSUMING the sub-agent iterator and no live frame
      //    ever reached the tab's WS subscription to trigger `ensureStreaming`
      //    (the reported "主停了、子还在跑、去子标签停按钮不变" bug). The
      //    TRUTHFUL "is this sub-agent running?" source is the backend session
      //    status surfaced on `subagentMeta.status` (kept in sync by
      //    `_openSubAgentTabInner` / `_refreshSubAgentTab`). So we treat the
      //    tab as cancellable when EITHER `tab.status === "streaming"` OR the
      //    backend still reports `running` — as long as the tab is not already
      //    in a terminal/aborting local state (which owns its own transition).
      // ② Sub-agent blocks (`message.subAgentBlocks[]`) on any tab whose
      //    `subagent_id === subagentId` AND that are cancellable. Same
      //    truth-source widening: a block normally cancellable at `running`,
      //    but a parent Stop may have rewritten it to `error/interrupted`
      //    (messageCommit.ts) even though the sub-agent is STILL running on
      //    the backend. When the truth-source says `running` we still allow
      //    the flip so the user's manual Stop on that block is not a no-op.
      //    We NEVER flip a block the backend confirms is done/error (no
      //    truth-source `running`) — State-Truth-First 铁律 2 (no regression):
      //    a genuinely-settled block stays settled.
      //
      // For each we remember the prior status so we can roll back if the
      // backend reports the sub-agent wasn't actually in-flight.
      type TabRollback = { tabId: TabId; prev: ChatTabStatus };
      type BlockRollback = {
        tabId: TabId;
        msgIdx: number;
        blockIdx: number;
        prev: SubAgentBlock["status"];
      };
      const tabRollbacks: TabRollback[] = [];
      const blockRollbacks: BlockRollback[] = [];
      // Backend truth-source: is this sub-agent still running per the last
      // authoritative status we saw? Checks every OPEN tab's `subagentMeta`
      // for this id AND the per-conversation `subAgentIndex` cache (which the
      // rail keeps refreshed even for closed tabs). Used to widen the two
      // scans below beyond the local `streaming`/`running` UI states so a stop
      // is never silently absorbed while the backend run is genuinely alive.
      const backendStillRunning = ((): boolean => {
        for (const t of this.tabs) {
          if (
            t.kind === "subagent" &&
            t.subagentMeta?.subagentId === subagentId &&
            t.subagentMeta?.status === "running"
          ) {
            return true;
          }
        }
        for (const entries of Object.values(this.subAgentIndex)) {
          for (const e of entries) {
            if (e.subagentId === subagentId && e.status === "running") {
              return true;
            }
          }
        }
        return false;
      })();
      // ── Dedupe: if EVERY surface that references this sub-agent is already
      //    `aborting`, the user is double-clicking; skip the POST entirely.
      //    "Already in flight" = there exists at least one surface currently
      //    in `aborting` AND none in cancellable (`streaming`/`running`).
      let anyAlreadyAborting = false;
      let anyCancellable = false;

      for (const tab of this.tabs) {
        if (
          tab.kind === "subagent" &&
          tab.subagentMeta?.subagentId === subagentId
        ) {
          if (tab.status === "aborting") {
            anyAlreadyAborting = true;
          } else if (
            tab.status === "streaming" ||
            // Truth-source widening: backend still running but the local tab
            // status never reached `streaming` (stuck at `idle` after a parent
            // Stop). Exclude `error` — a locally-errored take-over tab owns its
            // own terminal transition and must not be dragged into `aborting`.
            (backendStillRunning && tab.status !== "error")
          ) {
            tabRollbacks.push({ tabId: tab.id, prev: tab.status });
            anyCancellable = true;
          }
        }
        // Walk every message's subAgentBlocks (a sub-agent may appear on the
        // parent tab AND on its own standalone subagent tab — both should be
        // marked). We mark blocks that are currently running OR that the
        // backend still reports running (even if a parent Stop already
        // rewrote the block to `error/interrupted` locally). A block the
        // backend confirms is done/error is left untouched.
        for (let mi = 0; mi < tab.messages.length; mi += 1) {
          const msg = tab.messages[mi];
          const blocks = msg?.subAgentBlocks;
          if (blocks === undefined) continue;
          for (let bi = 0; bi < blocks.length; bi += 1) {
            const b = blocks[bi];
            if (b === undefined) continue;
            if (b.subagent_id !== subagentId) continue;
            if (b.status === "aborting") {
              anyAlreadyAborting = true;
            } else if (
              b.status === "running" ||
              // Truth-source widening: the backend still reports running, so a
              // block a parent Stop prematurely flipped to `error` is STILL
              // interruptible from the user's manual Stop. Never re-open a
              // `done` block, and never touch anything when the backend is not
              // running (no-regression: a genuinely-settled block stays put).
              (backendStillRunning && b.status === "error")
            ) {
              blockRollbacks.push({
                tabId: tab.id,
                msgIdx: mi,
                blockIdx: bi,
                prev: b.status,
              });
              anyCancellable = true;
            }
          }
        }
      }

      // Dedupe: already-aborting and nothing new to cancel → no POST.
      if (!anyCancellable && anyAlreadyAborting) {
        return true;
      }

      // ── Apply optimistic state (before awaiting the POST so the UI flips
      //    on the SAME synchronous tick the user clicked) ──────────────────
      for (const r of tabRollbacks) {
        this._patchTab(r.tabId, { status: "aborting" });
      }
      // Group block updates by (tabId, msgIdx) and rebuild each message's
      // `subAgentBlocks` array immutably (matches how `frameHandlers.ts`'
      // `patchSubAgentBlocks` updates them — one message replace per tab).
      const blocksByTabMsg = new Map<string, BlockRollback[]>();
      for (const r of blockRollbacks) {
        const key = `${r.tabId}\u0001${r.msgIdx}`;
        const list = blocksByTabMsg.get(key) ?? [];
        list.push(r);
        blocksByTabMsg.set(key, list);
      }
      for (const [key, list] of blocksByTabMsg) {
        const [tabId, msgIdxStr] = key.split("\u0001");
        if (tabId === undefined || msgIdxStr === undefined) continue;
        const tab = this.tabs.find((t) => t.id === tabId);
        if (tab === undefined) continue;
        const msgIdx = Number(msgIdxStr);
        const msg = tab.messages[msgIdx];
        if (msg?.subAgentBlocks === undefined) continue;
        const targetBlockIdxs = new Set(list.map((r) => r.blockIdx));
        const nextBlocks = msg.subAgentBlocks.map((b, bi) =>
          targetBlockIdxs.has(bi) ? { ...b, status: "aborting" as const } : b,
        );
        const nextMsg: ChatMessage = { ...msg, subAgentBlocks: nextBlocks };
        const nextMessages = [
          ...tab.messages.slice(0, msgIdx),
          nextMsg,
          ...tab.messages.slice(msgIdx + 1),
        ];
        this._patchTab(tabId, { messages: nextMessages });
      }

      // ── Helper: roll back to the captured prior status. Called on
      //    aborted=false or network error. Re-reads the live tab state at
      //    rollback time so we don't clobber a terminal frame that arrived
      //    between optimistic-mark and POST-resolve (only rolls back surfaces
      //    that are STILL `aborting`; ones that have already settled to
      //    done/error are left alone). */
      const rollback = (): void => {
        for (const r of tabRollbacks) {
          const live = this.tabs.find((t) => t.id === r.tabId);
          if (live === undefined) continue;
          if (live.status === "aborting") {
            this._patchTab(r.tabId, { status: r.prev });
          }
        }
        // Roll back blocks: same per-message immutable rebuild as forward
        // path, only flipping entries that are STILL `aborting`. Each block is
        // restored to ITS captured prior status (`running` in the common case,
        // or `error` for a block we optimistically re-opened on the
        // truth-source-widened path — see the collection scan above).
        for (const [key, list] of blocksByTabMsg) {
          const [tabId, msgIdxStr] = key.split("\u0001");
          if (tabId === undefined || msgIdxStr === undefined) continue;
          const tab = this.tabs.find((t) => t.id === tabId);
          if (tab === undefined) continue;
          const msgIdx = Number(msgIdxStr);
          const msg = tab.messages[msgIdx];
          if (msg?.subAgentBlocks === undefined) continue;
          const prevByBlockIdx = new Map(list.map((r) => [r.blockIdx, r.prev]));
          let mutated = false;
          const nextBlocks = msg.subAgentBlocks.map((b, bi) => {
            const prev = prevByBlockIdx.get(bi);
            if (prev !== undefined && b.status === "aborting") {
              mutated = true;
              return { ...b, status: prev };
            }
            return b;
          });
          if (!mutated) continue;
          const nextMsg: ChatMessage = { ...msg, subAgentBlocks: nextBlocks };
          const nextMessages = [
            ...tab.messages.slice(0, msgIdx),
            nextMsg,
            ...tab.messages.slice(msgIdx + 1),
          ];
          this._patchTab(tabId, { messages: nextMessages });
        }
      };

      const { apiJson } = await import("@/api");
      try {
        const res = await apiJson<{ ok: boolean; aborted: boolean }>(
          "POST",
          `/api/chat/subagents/${encodeURIComponent(subagentId)}/interrupt`,
        );
        if (res.aborted === true) {
          // Backend signalled the sub-agent — keep `aborting` and wait for
          // the terminal frame / snapshot refresh to settle the UI.
          //
          // Fix 4 (E-1 defence-in-depth) — DO NOT rely SOLELY on the sub-agent
          // WS `done`/close to settle the tab. The settle path
          // (`_subscribeSubAgentStream` → WS `done`/close → `refreshFromSnapshot`
          // → confirmDone/confirmAbort) depends on a terminal frame that a
          // backend cancel path could historically fail to emit (root cause:
          // `agent_tool.py` iter_events finally not marking the broadcaster
          // terminal). Even with the backend fixed, add a bounded, WS-INDEPENDENT
          // fallback: after a short grace window, if any surface for this
          // sub-agent is STILL `aborting`, pull the authoritative persisted
          // snapshot and settle from `detail.status` (interrupted/done/error).
          // `_refreshSubAgentTab` re-reads the tab and applies the terminal
          // transition; idempotent when the WS already settled it (the tab is
          // no longer `aborting` → the guard below skips). This makes the tab
          // self-heal without a WS frame.
          window.setTimeout(() => {
            for (const t of this.tabs) {
              if (
                t.kind === "subagent" &&
                t.subagentMeta?.subagentId === subagentId &&
                t.status === "aborting"
              ) {
                // Still stuck stopping after the grace window → settle from the
                // persisted snapshot (no WS frame required).
                this.confirmAbort(t.id);
                void this._refreshSubAgentTab(t.id, subagentId, {
                  preserveLiveMessages: true,
                });
              }
            }
            // ── Fix (card spins forever) — settle INLINE SubAgentBlocks too ──
            // The block rendered inside the PARENT chat tab (an assistant turn's
            // `subAgentBlocks[]`) is flipped to `aborting` by the optimistic
            // step above, but the parent tab has NO per-child WS subscription,
            // so no `subagent_done`/`subagent_error` frame ever reaches it to
            // clear the spinner. Only the standalone `kind:"subagent"` tab has a
            // WS. Without this, the inline card spins forever after Stop. Settle
            // every still-spinning inline block for this sub-agent to `done`
            // (WS-independent), mirroring `frameHandlers.handleSubagentDone`.
            // Idempotent: blocks already settled (done/error) are left alone.
            this._settleInlineSubAgentBlocks(subagentId);
          }, 2500);
          return true;
        }
        // Backend reports nothing was in flight (already done / not found):
        // roll back so the UI doesn't show a phantom "stopping…".
        rollback();
        return false;
      } catch {
        rollback();
        return false;
      }
    },

    /**
     * Open + drive the live SSE subscription for a sub-agent tab (block 2).
     *
     * Independent of the parent conversation stream: connects to the
     * `WS /api/chat/subagents/{id}/ws` WebSocket (migrated from the legacy
     * `GET …/stream` SSE so concurrent sub-agent tabs don't exhaust the
     * browser's ~6 per-host HTTP/1.1 connections) and normalises each `frame`
     * envelope's `subagent_*` payload into the equivalent MAIN-AGENT
     * `ChatStreamFrame` (`subAgentEventToChatFrame`), then drives it through
     * the SAME `applyFrame` → `FRAME_HANDLERS` pipeline the main agent uses —
     * so the live view renders identical round messages + tool cards (one
     * render path, no bespoke text trace). On terminal
     * (`subagent_done`/`subagent_error`/`done`) the tab is refreshed from the
     * authoritative persisted snapshot (via the shared `mapHistoryItems`
     * mapper) so the final transcript matches a later reopen.
     *
     * Reentrancy: any prior subscription for the tab is aborted first
     * (`attachSubAgentStream` aborts the controller → closes the prior WS).
     * The subscription is torn down on tab close (`clearSubAgentStream`).
     */
    _subscribeSubAgentStream(tabId: TabId, subagentId: string): void {
      // Perf (B1): mark this tab LIVE at subscription ENTRY — BEFORE
      // `attachSubAgentStream` aborts any prior controller (whose `onAbort`
      // must NOT clear the marker, see below) and BEFORE the async WS connects
      // and delivers its first frame. Marking at entry (not lazily on the first
      // frame) closes a window: the same-process cursor-0 backfill can begin
      // burst-delivering frames the instant the WS opens, and a resume
      // re-subscribe re-enters here synchronously — so the tab must already be
      // on the live tier when the very first `appendRoundChunk` runs, otherwise
      // that first burst塌缩 onto the coarse 150ms background timer. Idempotent.
      // The marker is cleared ONLY on a genuine terminal / tab-close (not on the
      // re-subscribe abort), so runs 2..N keep streaming live without a gap.
      //
      // Crit-3: pass `subagentId` so the composite-key transient handles
      // isolate multi-sub-agent state per (tabId, subagentId).
      // Without it, a second sub-agent subscribing on the same parent tab P
      // would land on the legacy LEGACY_SUBAGENT_KEY slot, abort the first
      // sub-agent's controller (via `attachSubAgentStream`'s prior-abort
      // semantics), and tear down its live marker — multi-subagent WS互踩.
      markSubAgentTabLive(tabId, subagentId);

      // 方案② — SKIP-BROADCAST-WS-DURING-TAKEOVER.
      //
      // When THIS tab has an ACTIVELY-running MAIN transport
      // (`peekTransport(tabId)?.isInFlight() === true`), a live take-over turn
      // the user started in this sub-agent tab is streaming on the main
      // transport (`useChatTransport` → `/api/chat/ws?subagent_id=…`), NOT on
      // the sub-agent broadcaster. Re-subscribing the broadcaster WS here is
      // pure REDUNDANCY that only hurts:
      //   * The broadcaster entry for this sub-agent id holds the PREVIOUS
      //     parent-spawned run's frames (already `trim`ped + `mark_terminal`ed
      //     on the backend — see `sub_agent_stream_broadcaster.py` /
      //     `agent_tool.py`; the take-over turn NEVER publishes to it). So the
      //     WS would replay a STALE cursor-0 backfill + a STALE terminal
      //     `done` — none of which belongs to the live take-over.
      //   * That stale backfill, applied through `applyFrame`, can pollute the
      //     take-over transcript AND advance the `from_seq` cursor
      //     (`recordSubAgentAppliedSeq`) to a stale sequence, mis-aligning a
      //     LATER genuine re-subscribe.
      // The live take-over's frames + terminal settle are owned end-to-end by
      // the main transport (`applyFrame` / `confirmDone` / `confirmAbort` in
      // `useChatTransport`), and any grand (孙) sub-agent it spawns arrives
      // inline on that same main-transport stream — so skipping the broadcaster
      // WS loses NOTHING for the take-over case.
      //
      // Discriminator is the SAME source-of-truth as the settle guard in
      // `refreshFromSnapshot` below and ChatView's Stop path
      // (ChatView.vue:516-517): `isInFlight()`. A leftover-but-IDLE transport
      // (`isInFlight() === false`) does NOT skip — a spectator run (parent
      // spawned the sub-agent, user did NOT take over) subscribes normally, and
      // a WS-drop re-subscribe of a spectator run also re-connects (never
      // mis-skipped). The `markSubAgentTabLive` above already ran, so the tab's
      // live-tier semantics are preserved even on the skip path.
      //
      // NOTE: this is an ADDITIVE optimisation; the `refreshFromSnapshot`
      // take-over-in-flight guard below is intentionally retained as defence in
      // depth for the corner where a broadcaster WS is nonetheless connected.
      let takeoverInFlight = false;
      try {
        takeoverInFlight =
          useChatTransports().peekTransport(tabId)?.isInFlight() === true;
      } catch {
        // Non-component / test env where the transport singleton is
        // unavailable — fall back to the historical behaviour (subscribe).
        takeoverInFlight = false;
      }
      if (takeoverInFlight) {
        return;
      }

      const controller = new AbortController();
      attachSubAgentStream(tabId, controller, subagentId);

      // Drive the sub-agent's live events through the SAME `applyFrame` →
      // `FRAME_HANDLERS` pipeline the main agent uses, by normalising each
      // `subagent_*` event into the equivalent main-agent `ChatStreamFrame`
      // (`subAgentEventToChatFrame`). This renders REAL tool cards live
      // (identical to the main agent) instead of a bespoke `🔧 toolname` text
      // trace. `applyFrame` requires the tab to be `streaming`, so flip it
      // on first frame; the authoritative snapshot refresh on close resets it.
      let seq = 0;
      let startedStreaming = false;
      // Tracks whether the most recent frame applied through this subscription
      // was a BACKFILL (cursor=0 replay) frame. The first live frame after a
      // backfill burst — or the WS `done` envelope, or an unexpected close —
      // is the boundary where the accumulated backfill buffer must be
      // committed (see `flushBackfill`).
      //
      // ── Honest semantics (the paint count is "per contiguous chunk run",
      //    NOT a single paint for the whole burst) ─────────────────────────
      // `applyFrame` synchronously flushes coalescing buffers before every
      // NON-chunk frame (`chatTabs.ts` ~line 1977-1987) so handlers observe
      // up-to-date state (the `tool_call` handler reads/clears
      // `streamingContent`, and the round-open branch reads `lead_in` —
      // ordering invariants that are NOT safe to relax for backfill). So a
      // backfill burst that interleaves chunks with tool_call / tool_result
      // frames produces ONE paint per CONTIGUOUS RUN of chunks (≤
      // 1 + (#non-chunk-frames-in-burst) paints in total), NOT a single
      // batched paint for the whole transcript-so-far.
      //
      // **Why this is still the correct fix**: the user's reported bug was
      // "trailing transcript replayed逐 frame" — typewriter-level,
      // character-by-character paints — which we DID eliminate (each chunk
      // run now lands instantly as a block). Going from "N chunk paints"
      // (typewriter feel) to "K block paints, K ≤ #non-chunk frames + 1"
      // (instant block reveal) crosses the threshold of perceptual
      // batching: the user sees text blocks appear, not characters typing.
      // For a typical 3-round backfill with one tool_call per round, that
      // is ~3-4 block paints over <100ms — visually equivalent to a single
      // snapshot paint. Going to truly "one paint" would require relaxing
      // the chunk-before-tool_call ordering invariant, which is a separate
      // architectural decision with real ordering-bug risk (see G.17 in
      // PR-905 review notes).
      let inBackfill = false;

      const ensureStreaming = (): void => {
        if (startedStreaming) return;
        startedStreaming = true;
        // Re-assert the live marker on the first frame too (idempotent): covers
        // the case where a prior subscription's teardown unmarked the tab
        // between this subscription's entry and its first frame. The marker is
        // what keeps a watched-but-background sub-agent tab on the ~20fps
        // `SUBAGENT_LIVE_FLUSH_MS` tier instead of the coarse 150ms background
        // tier (the resume "一次性刷新" bug). An ACTIVE (foreground) tab ignores
        // the marker entirely and stays on per-frame rAF — the marker only
        // RAISES a background tab's tier, never lowers a foreground tab's.
        // Crit-3: composite key — see entry-time markSubAgentTabLive above.
        markSubAgentTabLive(tabId, subagentId);
        // ── Fresh vs Resume — root-cause fix for the "切走切回断段" bug ─────
        //
        // Every entry into this subscription is one of two things:
        //   (i)  FRESH subscribe — the tab was just created (`openSubAgentTab`
        //        non-reuse path, `_openSubAgentTabInner`; `_hydrateRestoredSubAgentTab`
        //        after a page reload). `tab.status === "idle"` because the tab
        //        was just built from the HTTP snapshot and has NO in-memory
        //        round routing.
        //   (ii) RESUME subscribe — the tab was already open, the user
        //        switched away and back (`openSubAgentTab` reuse path), or a
        //        cross-tab `subagent_start` wake fired for an already-open
        //        standalone tab. `tab.status` is `streaming` (the sub-agent
        //        is still producing) and `roundMessageIds` etc. hold the
        //        LIVE round-to-message mapping the last chunks were
        //        appending onto.
        //
        // Historical bug: this code unconditionally called `setStreaming`
        // (fresh-turn semantics — wipe ALL round routing), which was
        // correct for (i) but destructive for (ii): the very next chunk
        // frame, still stamped with the SAME `round_index` as the one
        // that was streaming before, found `roundMessageIds[ri]` undefined
        // in `frameHandlers.handleChunk` and opened a spurious SECOND
        // assistant message, leaving the previously-accumulated content
        // orphaned in `tab.messages` (visible as "split into two bubbles";
        // closing + reopening the tab re-loaded the persisted (correctly
        // merged) transcript from HTTP, which is why the DB was always
        // correct).
        //
        // `resumeStreaming` (new 2026-07-14) is the fix: it promotes
        // `idle → streaming` for case (i) and is a no-op for the
        // `streaming` case (ii), leaving round routing intact so the next
        // chunk lands on the correct pre-existing message. See the doc
        // block on `resumeStreaming` in this file for the full semantics.
        //
        // Prior fix layers (kept, complementary, NOT superseded):
        //   • Backend `from_seq=<lastApplied+1>` on the WS URL prevents
        //     REDELIVERED frames (broadcaster replay of frames already
        //     applied). It does NOT prevent NEW chunks (arriving after the
        //     re-subscribe) from being mis-routed — that is what this
        //     fresh-vs-resume split fixes.
        //   • The `takeoverInFlight` early-return above skips this whole
        //     function during a user's active take-over turn, so the
        //     take-over path was accidentally shielded from the old bug;
        //     the resume-vs-fresh split makes that shielding explicit for
        //     the spectator path too.
        //   • PRESERVING `messages` (the wipe never touched `messages`;
        //     see the earlier fix that removed `messages: []` from the
        //     wipe). The `resumeStreaming` fix leaves BOTH `messages`
        //     AND round routing intact, so the last streaming bubble
        //     continues to grow instead of being orphaned.
        this.resumeStreaming(tabId);
      };

      /** Commit any backfill text that has been accumulating in the
       *  coalescing buffers (suppressed-flush mode) into reactive state in ONE
       *  synchronous reactive write. Called at the backfill→live boundary (the
       *  first live frame after a cursor=0 replay) and at the terminal `done`
       *  envelope, so the trailing transcript the user already saw rendered
       *  via the HTTP snapshot lands as a single paint — never replayed逐段. */
      const flushBackfill = (): void => {
        if (!inBackfill) return;
        inBackfill = false;
        this.flushRoundChunkNow(tabId);
        this.flushStreamingNow(tabId);
      };

      const handleFrame = (data: unknown): void => {
        if (data === null || typeof data !== "object") return;
        // WS frame envelope shape (see `interfaces/http/routes/chat/_ws.py`
        // `subagent_ws` + broadcaster `_replay_with_entry`):
        //   { type:"frame", event:"frame",
        //     payload: { sequence, payload: <subagent_* event>, backfill? } }
        // i.e. the broadcaster wraps the actual `subagent_*` event under its OWN
        // `payload` key (alongside `sequence`/`backfill`), and the WS route wraps
        // THAT under the envelope's `payload`. So the real event is at
        // `data.payload.payload` — NOT `data.payload`. (A prior version read one
        // level too shallow, so `ev.type` was undefined → `subAgentEventToChatFrame`
        // returned null → EVERY live frame was silently dropped and the tab only
        // updated via the terminal snapshot refresh: the reported "切过去等一会、
        // 工具调用结束后才一次性刷出" bug. The frames WERE arriving live; we just
        // weren't rendering them.)
        const envelope = (data as { payload?: unknown }).payload;
        if (envelope === null || typeof envelope !== "object") return;
        const frameMeta = envelope as Record<string, unknown>;
        const inner = frameMeta.payload;
        if (inner === null || typeof inner !== "object") return;
        const ev = inner as Record<string, unknown>;
        // LIVE token-usage refresh (bug ① fix): the sub-agent kernel
        // tail-appends `used_tokens` (the provider-measured wire size of the
        // round just completed — State-Truth-First, NOT an estimate) +
        // `context_limit` (the sub-agent's OWN model window, resolved from its
        // real `model_hint` in `agent_tool.py`) onto `subagent_tool_result`
        // (every tool round) and `subagent_done` (terminal fallback). Patch the
        // tab's `subagentMeta` so the context badge refreshes per round WHILE
        // the sub-agent runs, instead of only on open / terminal snapshot. We
        // do this BEFORE the render-frame branch because `subagent_done`
        // produces no render frame (it returns null from
        // `subAgentEventToChatFrame`) yet still carries the terminal figure.
        if (ev.type === "subagent_tool_result" || ev.type === "subagent_done") {
          const liveUsed = ev.used_tokens;
          const liveLimit = ev.context_limit;
          if (typeof liveUsed === "number" && liveUsed >= 0) {
            const tab = this.tabs.find((t) => t.id === tabId);
            if (tab !== undefined && tab.subagentMeta !== undefined) {
              const limit =
                typeof liveLimit === "number" && liveLimit > 0
                  ? liveLimit
                  : tab.subagentMeta.budgetTokens;
              const denom =
                typeof limit === "number" && limit > 0 ? limit : 0;
              // Raw (un-clamped)口径 — same as the main agent: the badge can
              // show >100% when the sub-agent's history exceeds its window.
              const rawRatio = denom > 0 ? liveUsed / denom : 0;
              const clampedUsed = denom > 0 ? Math.min(liveUsed, denom) : 0;
              const clampedRatio = denom > 0 ? clampedUsed / denom : 0;
              this._patchTab(tabId, {
                subagentMeta: {
                  ...tab.subagentMeta,
                  usedTokens: clampedUsed,
                  rawUsedTokens: liveUsed,
                  ratio: clampedRatio,
                  rawRatio,
                  ...(typeof limit === "number" && limit > 0
                    ? { budgetTokens: limit }
                    : {}),
                },
              });
            }
          }
        }
        // BACKFILL frames are the transcript-so-far that the broadcaster had
        // already buffered when this (possibly late / re-subscribing) WS
        // connected — NOT live deltas the user can watch appear. We accumulate
        // chunk text into the coalescing buffers (via the suppressed
        // `appendRoundChunk(..., backfill=true)` path) without scheduling any
        // flush; at the backfill→live boundary (the first frame with
        // `backfill=false`) OR at the WS `done` / unexpected close, the
        // accumulated batch is committed in ONE flush.
        //
        // **Paint count: ≤ 1 + (#non-chunk-frames-in-burst), not literally 1**
        // (see the `inBackfill` declaration above for the full rationale).
        // Each contiguous run of chunk frames lands as one paint; an
        // intervening non-chunk frame triggers a sync flush (line ~1977-1987)
        // to preserve chunk-before-tool_call ordering invariants. This still
        // eliminates the user-reported typewriter replay (the original bug
        // was per-FRAME paints, character-level; we now paint at block
        // boundaries, instant-reveal). Live frames (backfill omitted) flow
        // through the normal rAF/timer tier unchanged.
        const isBackfill = frameMeta.backfill === true;
        if (inBackfill && !isBackfill) {
          // Boundary: backfill burst ended, live frames now begin. Commit the
          // accumulated history in one write BEFORE the first live frame
          // reaches the reducer, so the live append sits on top of the
          // already-committed history rather than after still-pending buffer.
          flushBackfill();
        }
        inBackfill = isBackfill;
        // `subagent_start` carries the task prompt (prompt_preview) but no
        // render frame. Use it to seed the user turn after the message reset so
        // the cursor-0 live replay shows the task, then the assistant frames.
        if (ev.type === "subagent_start") {
          ensureStreaming();
          const preview =
            typeof ev.prompt_preview === "string" ? ev.prompt_preview : "";
          if (preview !== "") {
            const tab = this.tabs.find((t) => t.id === tabId);
            // Seed THIS run's task as a user turn so the live view shows
            // "task → assistant frames" (parity with the inline block). We now
            // PRESERVE prior history (no `messages: []` wipe), so guard against
            // re-seeding: only append the task when the current last message is
            // not already this exact task text (covers both the fresh-open case
            // — empty list — and the resume case, where the prior run's
            // transcript is the preserved prefix and run-N's task is a NEW user
            // turn appended after it). The terminal `refreshFromSnapshot`
            // reconciles to the authoritative transcript regardless.
            if (tab !== undefined) {
              const last = tab.messages[tab.messages.length - 1];
              const alreadySeeded =
                last !== undefined &&
                last.role === "user" &&
                last.content === preview;
              if (!alreadySeeded) {
                this._patchTab(tabId, {
                  messages: [
                    ...tab.messages,
                    {
                      id: `${subagentId}:task:${seq}`,
                      role: "user",
                      content: preview,
                      createdAt: Date.now(),
                      ...(tab.conversationId != null
                        ? { conversationId: tab.conversationId }
                        : {}),
                    },
                  ],
                });
              }
            }
          }
          return;
        }
        const frame = subAgentEventToChatFrame(ev, seq);
        if (frame === null) return;
        ensureStreaming();
        seq += 1;
        // Snapshot the tab's status BEFORE applyFrame so we know whether
        // applyFrame's entry gate (chatTabs.ts:~2388 — must be "streaming")
        // will accept this frame. If the tab was flipped to "aborting" /
        // "error" between the last frame and this one (interrupt / mid-turn
        // reconnect / handler-driven terminal), applyFrame will drop this
        // frame WITHOUT touching messages — and the from_seq tracker
        // MUST NOT advance past a frame that was never rendered, or a
        // later re-subscribe would silently swallow every frame between
        // the flip and the reconnect (permanent hole in the transcript).
        //
        // `ensureStreaming()` above already forced status="streaming" on
        // the FIRST frame of this subscription, so the entry gate is
        // guaranteed to pass on frame 1. Subsequent frames observe the
        // real status — the check below is what disambiguates.
        const tabAtEntry = this.tabs.find((t) => t.id === tabId);
        const willBeApplied =
          tabAtEntry !== undefined && tabAtEntry.status === "streaming";
        // Reuse the main-agent reducer pipeline verbatim — one render path.
        // Forward the `isBackfill` flag so chunk handlers route through the
        // suppressed-flush path (no per-frame reactive write); live frames
        // (isBackfill=false) keep the existing rAF/timer tier.
        this.applyFrame(tabId, frame, isBackfill);
        // Advance the from_seq tracker ONLY when applyFrame actually
        // applied the frame (entry gate opened). `frameMeta.sequence` is
        // the broadcaster's monotonic sequence — see
        // `SubAgentStreamBroadcaster.register` docstring (monotonic across
        // the sub-agent id's whole lifetime, spanning resumes). Missing /
        // non-numeric → skip (defensive; the wire always carries a seq).
        const rawSeq = frameMeta.sequence;
        if (
          willBeApplied &&
          typeof rawSeq === "number" &&
          Number.isFinite(rawSeq)
        ) {
          recordSubAgentAppliedSeq(tabId, subagentId, rawSeq);
        }
      };

      // Refresh the sub-agent's META (status / usage / model) once the stream
      // closes, and reset the tab out of the streaming status the live frames
      // set. The MESSAGES are NOT re-fetched/replaced here: the live stream
      // already rendered the authoritative transcript and the backend persisted
      // the complete wire, so a terminal full re-map would be redundant O(N)
      // work (and historically risked clobbering correct live content). A
      // re-open / reload (no live) still reads the snapshot via the same method.
      const refreshFromSnapshot = (): void => {
        // Terminal (or unexpected close): this tab is no longer a live stream,
        // so drop its live-tier marker (it returns to the normal background
        // tier for any future activity). Idempotent.
        // Crit-3: composite key — only clear THIS (tabId, subagentId) slot.
        // A single-arg call would wipe the live marker for every sub-agent
        // sharing the parent tab P, dropping concurrent siblings off the
        // live tier.
        unmarkSubAgentTabLive(tabId, subagentId);
        const tab = this.tabs.find((t) => t.id === tabId);
        if (tab !== undefined && tab.status === "streaming") {
          // SUBAGENT-STALE-DONE-TAKEOVER guard (regression fix for "主停级联
          // 停子 → 子标签重新提问跑新一轮 → 切走切回 → 新一轮被误停"):
          //
          // This terminal `done`/`error` came from the SUB-AGENT BROADCASTER
          // WS. But a `streaming` tab here can be driven by ONE of TWO very
          // different sources:
          //   (a) the sub-agent broadcaster stream itself (a parent-spawned
          //       run the user is watching live) — then this `done` is the
          //       real terminal and MUST settle via `confirmDone`; OR
          //   (b) a LIVE take-over turn the user just started in this tab,
          //       which runs on the MAIN transport (`useChatTransport` →
          //       `/api/chat/ws` with `?subagent_id=…`), NOT on the sub-agent
          //       broadcaster. Its terminal is owned by the main transport's
          //       own done/close → `confirmDone`/`confirmAbort`.
          //
          // After the cascade-stop fix, stopping the main agent marks the
          // sub-agent's broadcaster entry `done=True` (agent_tool.py
          // `mark_terminal`) and the broadcaster keeps that terminal entry for
          // its TTL (~600s). A later `openSubAgentTab` reuse re-subscribes the
          // broadcaster WS (chatTabs.ts ~2849) while a NEW take-over turn is
          // live in the tab; the broadcaster immediately replays that STALE
          // `done` → we land here with `status === "streaming"` and would
          // wrongly `confirmDone` the live take-over → the model "stops" with
          // the user never pressing Stop (the reported regression).
          //
          // Truth-source discriminator (SAME as ChatView's Stop path,
          // ChatView.vue:516-520): if THIS tab has an ACTIVELY-running main
          // transport (`peekTransport(tabId)?.isInFlight()`), the `streaming`
          // status belongs to the live take-over (case b) and the sub-agent
          // broadcaster's `done` is stale → do NOT settle it here (the main
          // transport owns the terminal). Only the cheap meta refresh below
          // runs. When no transport is in flight (case a — a genuine
          // broadcaster-driven run, or an ordinary open with no take-over), we
          // settle normally via `confirmDone` — no regression.
          let takeoverInFlight = false;
          try {
            takeoverInFlight =
              useChatTransports().peekTransport(tabId)?.isInFlight() === true;
          } catch {
            // Non-component / test env where the transport singleton is
            // unavailable — fall back to the historical behaviour (settle).
            takeoverInFlight = false;
          }
          if (!takeoverInFlight) {
            // Commit the in-flight live round into messages (authoritative)
            // and leave the streaming status.
            this.confirmDone(tabId);
          }
          // else: live take-over on the main transport owns the terminal —
          // skip settling here; only refresh the sub-agent meta below.
        } else if (tab !== undefined && tab.status === "aborting") {
          // User-initiated stop path: `interruptSubAgent` flipped status to
          // "aborting" while waiting for the backend's terminal frame. Once
          // that frame arrives (WS closed → we reach here), the abort needs
          // the SAME treatment as the main agent's abort path —
          // `confirmAbort` rewrites in-flight tool / sub-agent blocks to
          // `aborted` status, clears the streaming buffers, and resets
          // `tab.status` to "idle". Just patching `status: "idle"` here
          // (an earlier draft) left tool cards spinning forever — only a
          // foreground tab-switch elsewhere would re-trigger a render and
          // visually "fix" them, hence the reported "切走切回工具卡才不转
          // 了". `confirmAbort` accepts both `streaming` AND `aborting`
          // (line 2656), so this is the right entry point.
          this.confirmAbort(tabId);
        } else if (tab !== undefined && tab.status === "error") {
          // Crit-2: sub-agent reached the snapshot path while its own tab
          // status is already "error" (e.g. `recordError` flipped it on
          // some prior error frame, or a take-over turn errored out).
          // `isActiveTabBusy` reads `streaming || aborting` so "error" is
          // NOT busy → the stop button does not stay engaged, but we still
          // want to finalize any in-flight tool / sub-agent blocks so they
          // do not visibly spin forever (symmetric with the abort path
          // above). `confirmAbort` is the right helper — it accepts
          // `streaming` and `aborting`, but NOT `error`, so we transition
          // through `streaming` momentarily by patching the in-flight
          // blocks directly via `buildConfirmAbortMessages` (the same
          // helper `confirmAbort` uses internally) and resetting streaming
          // buffers, leaving `tab.status` at `error` so the user keeps
          // their error indicator.
          //
          // In practice this branch is RARELY reachable — the live frame
          // handlers don't typically pre-flip `tab.status` to `"error"`
          // before WS close (they handle errors at the block / message
          // level, see `handleSubagentError` in frameHandlers.ts:1816). So
          // this is belt-and-suspenders for the corner case where some
          // future code path or take-over flow does set `error` before
          // refreshFromSnapshot runs.
          cancelStreamingFlush(tabId);
          this._flushStreamingBuffer(tabId);
          cancelRoundChunkFlush(tabId);
          this._flushRoundChunkBuffer(tabId);
          const errTab = this.tabs.find((t) => t.id === tabId) ?? tab;
          const messages = buildConfirmAbortMessages(
            errTab,
            nextMessageId(),
            false,
          );
          this._patchTab(tabId, {
            // Keep status: "error" — user-visible error indicator stays.
            streamingContent: "",
            streamingToolCalls: [],
            activeToolMessageId: null,
            roundMessageIds: {},
            roundSubAgentMessageIds: {},
            activeSubAgentMessageId: null,
            streamingUsage: null,
            streamingPerf: null,
            streamingSubAgentBlocks: [],
            streamingRequestId: null,
            compacting: false,
            messages,
            streamingSenderId: null,
            streamingSenderName: null,
            streamingSenderColor: null,
            streamingSenderModelId: null,
          });
        }
        // Meta-only refresh (status/usage/model); keeps the live-rendered
        // messages — see _refreshSubAgentTab(preserveLiveMessages).
        void this._refreshSubAgentTab(tabId, subagentId, {
          preserveLiveMessages: true,
        });
      };

      // Connect the sub-agent's live progress over a WebSocket (migrated from
      // SSE): a standalone sub-agent tab is read-only, but using WS means
      // concurrent sub-agent tabs do NOT each hold one of the browser's ~6
      // per-host HTTP/1.1 connections (the same reason the main chat stream is
      // WS). The server pushes `{type:"frame", event, payload}` envelopes
      // (broadcaster.replay: cursor-0 backfill THEN live frames) and a terminal
      // `{type:"done"}`/`{type:"error"}`, then closes. Frame `payload` has the
      // same shape the SSE path delivered, so `handleFrame` is unchanged.
      void (async () => {
        // Best-effort + non-DOM/test safe: if the API barrel doesn't expose
        // `wsBaseUrl` or the environment has no `WebSocket` (unit tests /
        // SSR), skip the live subscription silently — the snapshot the tab
        // already rendered on open remains valid (no unhandled rejection).
        let ws: WebSocket;
        try {
          const api = (await import("@/api")) as {
            wsBaseUrl?: () => string;
          };
          if (
            typeof api.wsBaseUrl !== "function" ||
            typeof globalThis.WebSocket !== "function"
          ) {
            return;
          }
          const base = api.wsBaseUrl();
          // Sub-agent WS from_seq (block 2 — root-cause fix for "切走切回
          // 子 Agent 标签之前已渲染的部分再次重播" 2026-07-01): pass the
          // highest broadcaster sequence THIS (tabId, subagentId) has ever
          // applied so the server elides frames the tab already rendered.
          // On a first subscribe / cold open there is no prior state, so
          // `getSubAgentLastAppliedSeq` returns -1 and we send `from_seq=0`
          // — byte-parity with the historical contract (full cursor-0
          // replay). On a re-subscribe (tab-switch reuse / WS drop
          // reconnect / layout restore) the tracker holds the highest
          // sequence the tab actually rendered → server replay starts
          // right after it → no echo, no client-side dedupe, no闪烁
          // (existing messages stay put; only frames the client has NOT
          // seen arrive on the wire). Mirrors the main-agent `from_seq`
          // design for `active_run_ws`.
          const lastApplied = getSubAgentLastAppliedSeq(tabId, subagentId);
          const fromSeq = lastApplied >= 0 ? lastApplied + 1 : 0;
          const query = fromSeq > 0 ? `?from_seq=${fromSeq}` : "";
          const path = `/api/chat/subagents/${encodeURIComponent(subagentId)}/ws${query}`;
          const url = base === "" ? path : `${base}${path}`;
          ws = new WebSocket(url);
        } catch {
          // Import failed / ctor threw (bad URL / unsupported) — best-effort.
          return;
        }
        // Tab close → `clearSubAgentStream` aborts the controller (and clears
        // the live-tier marker itself); a resume RE-SUBSCRIBE also aborts the
        // prior controller via `attachSubAgentStream`. So `onAbort` must NOT
        // clear the live marker: on a re-subscribe the new subscription has
        // already re-marked the tab at its entry, and clearing here would race
        // it back off the live tier. We only close the prior WS so the server's
        // replay generator unwinds and releases its subscriber.
        const onAbort = (): void => {
          try {
            ws.close();
          } catch {
            // ignore
          }
        };
        if (controller.signal.aborted) {
          onAbort();
          return;
        }
        controller.signal.addEventListener("abort", onAbort, { once: true });

        let terminal = false;
        ws.addEventListener("message", (mev: MessageEvent) => {
          const raw = mev.data;
          if (typeof raw !== "string") return;
          let parsed: unknown;
          try {
            parsed = JSON.parse(raw);
          } catch {
            return;
          }
          if (parsed === null || typeof parsed !== "object") return;
          const env = parsed as Record<string, unknown>;
          const kind = env.type;
          if (kind === "frame") {
            // The frame envelope wraps the broadcaster payload under `payload`
            // (same shape the SSE `onFrame` received), so reuse `handleFrame`.
            handleFrame(env);
            return;
          }
          if (kind === "done" || kind === "error") {
            // Terminal: refresh from the authoritative persisted snapshot and
            // stop. The server closes right after; guard against a double
            // refresh from the subsequent `close` event.
            terminal = true;
            // Commit any backfill text still buffered (cold-open of a done
            // sub-agent: the broadcaster replays the full transcript marked
            // backfill, then immediately closes — no live frame ever arrives,
            // so the backfill→live boundary path never fires). Without this
            // the trailing transcript would stay in the suppressed buffer and
            // never paint. Idempotent / no-op when not in backfill mode.
            flushBackfill();
            refreshFromSnapshot();
          }
        });
        ws.addEventListener("close", () => {
          controller.signal.removeEventListener("abort", onAbort);
          if (!terminal && !controller.signal.aborted) {
            // Unexpected close (network / server drop) without a terminal
            // envelope: fall back to the persisted snapshot so the tab still
            // shows the latest authoritative transcript. Commit any pending
            // backfill batch first so it doesn't get clobbered by the snapshot.
            flushBackfill();
            refreshFromSnapshot();
          }
        });
        ws.addEventListener("error", () => {
          // The `close` handler runs next and handles the fallback refresh.
        });
      })();
    },

    /**
     * Re-fetch a sub-agent's persisted snapshot and replace the tab's
     * messages with the authoritative rendered transcript (block 2 — called
     * when the live stream terminates). Best-effort: a fetch failure leaves
     * the live-accumulated messages in place.
     */
    async _refreshSubAgentTab(
      tabId: TabId,
      subagentId: string,
      opts: { preserveLiveMessages?: boolean } = {},
    ): Promise<void> {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) return;
      const { apiJson } = await import("@/api");
      interface SubAgentMessageItem {
        role: string;
        text?: string | null;
        tool_calls?: ChatToolCall[] | null;
        created_at?: string | null;
        // 回看 parity (per-round request_id + usage on the assistant turn).
        usage?: ChatMessageUsage | null;
        meta?: Record<string, unknown> | null;
      }
      interface SubAgentDetail {
        subagent_id: string;
        status: string;
        owner: string;
        root_conversation_id: string;
        parent_subagent_id?: string | null;
        depth?: number;
        created_at?: string;
        updated_at?: string;
        messages?: SubAgentMessageItem[];
        // Sub-agent's OWN context usage (badge fix) — refreshed here so the
        // badge reflects the grown wire history once a run terminates.
        used_tokens?: number;
        budget_tokens?: number;
        ratio?: number;
        // REAL (un-clamped) occupancy — same口径 as the main agent (§3.1).
        raw_used_tokens?: number;
        raw_ratio?: number;
        // Sub-agent's OWN model (§3.1 tail-appended) — refreshed here so the
        // dropdown + budget stay aligned with the session truth source.
        model_id?: string | null;
        model_provider?: string | null;
      }
      // Re-resolve the budget against the sub-agent tab's own (parent-derived)
      // model so the refreshed usage uses the same window as the open path.
      const parentModelId = tab.modelId ?? null;
      const detailQuery =
        parentModelId !== null &&
        parentModelId !== "" &&
        parentModelId !== "qai-default"
          ? `?model_id=${encodeURIComponent(parentModelId)}`
          : "";
      let detail: SubAgentDetail;
      try {
        detail = await apiJson<SubAgentDetail>(
          "GET",
          `/api/chat/subagents/${encodeURIComponent(subagentId)}${detailQuery}`,
        );
      } catch {
        return;
      }
      // Reuse the SAME main-agent history mapper as `openSubAgentTab` so the
      // refreshed (authoritative) snapshot renders identical tool cards — the
      // backend shapes `messages` like main-agent history rows (results merged
      // onto `tool_calls[i].output`), so one mapper covers both.
      // Base timestamps on the sub-agent's CREATION time (turn 0 = start),
      // not `updated_at` (last round). Same rationale as `openSubAgentTab`.
      const baseIso = detail.created_at ?? detail.updated_at;
      const baseTs = baseIso ? Date.parse(baseIso) : Date.now();
      const createdMs = Number.isFinite(baseTs) ? baseTs : Date.now();
      const historyItems = (detail.messages ?? []).map((m, i) => ({
        id: `${subagentId}:${i}`,
        role: m.role,
        text: typeof m.text === "string" ? m.text : "",
        created_at:
          typeof m.created_at === "string" && m.created_at
            ? m.created_at
            : new Date(createdMs + i).toISOString(),
        parent_id: null,
        ...(m.tool_calls && m.tool_calls.length > 0
          ? { tool_calls: m.tool_calls }
          : {}),
        // 回看 parity: per-round request_id + usage on the assistant turn.
        ...(m.usage != null ? { usage: m.usage } : {}),
        ...(m.meta != null ? { meta: m.meta } : {}),
      }));
      const messages: ChatMessage[] = mapHistoryItems(
        historyItems,
        detail.root_conversation_id,
      );
      // ── Non-degradation guard (State-Truth-First 铁律 1) ──────────────
      // This refresh runs at terminal time and OVERWRITES the tab's messages
      // with the backend snapshot. But the persisted snapshot can momentarily
      // LAG the live stream we just rendered (the final summary text and/or the
      // last round's tool result may not be in the DB yet when `subagent_done`
      // arrives). Overwriting then would replace richer live content with a
      // poorer snapshot — the user sees the tool card + summary vanish (the bug
      // reported when OPENING a sub-agent tab). So: only adopt the snapshot's
      // `messages` when it is NOT poorer than what is already rendered. The
      // meta/model/usage updates below are always safe and applied regardless.
      // A later snapshot (status poll / re-open) reconciles once the DB catches
      // up, so we never get permanently stuck on stale live content.
      const current = this.tabs.find((t) => t.id === tabId);
      const currentMessages = current?.messages ?? [];
      // Terminal refresh (``preserveLiveMessages``): the live stream just
      // rendered the authoritative transcript and the backend has persisted the
      // complete wire (take-over rounds included), so there is NOTHING to gain
      // from re-mapping + replacing the whole message array — it was redundant
      // O(N) work that only risked clobbering correct live content. We now keep
      // the live messages as-is and refresh ONLY the cheap meta (status / usage
      // / model). Re-open / backfill (no live) still adopts the snapshot below.
      // The ``_snapshotWouldLoseContent`` guard remains the defensive backstop
      // for the re-open path.
      const adoptMessages =
        !opts.preserveLiveMessages &&
        !_snapshotWouldLoseContent(currentMessages, messages);
      // Update the subagentMeta status too so the stop button reflects state.
      // Sub-agent's OWN model is the session truth source: when the detail
      // carries a concrete model_id, keep the tab's modelId/modelProvider +
      // subagentMeta in sync with it (the dropdown + budget follow the session).
      const refModelId =
        typeof detail.model_id === "string" && detail.model_id !== ""
          ? detail.model_id
          : null;
      const refModelProvider =
        typeof detail.model_provider === "string" ? detail.model_provider : "";
      this._patchTab(tabId, {
        // Adopt the authoritative snapshot transcript ONLY on re-open/backfill
        // (no live) AND when it does not lose content vs what is rendered. The
        // terminal path keeps the live-rendered messages (see above).
        ...(adoptMessages
          ? { messages, todoList: extractLatestTodoList(messages) }
          : {}),
        // Crit-8: only overwrite `tab.modelId` / `tab.modelProvider` on a
        // SUB-AGENT tab (`kind === "subagent"`, where the tab IS the
        // sub-agent, so its top-level modelId IS the sub-agent's model and
        // must follow the authoritative detail). For an ordinary chat tab
        // the top-level modelId is the MAIN agent's model and must NOT be
        // clobbered.
        //
        // SUBA-MODEL-RACE-1 guard: when a `setSubAgentModel` PATCH is in
        // flight for THIS sub-agent, the GET response above may have read
        // the snapshot BEFORE the PATCH committed, so `detail.model_id` is
        // stale relative to what the user just picked. Skip the modelId
        // write in that case — the in-flight PATCH itself will write the
        // authoritative new value when it completes, so we don't lose
        // anything. The non-modelId fields (status / usage / etc.) are
        // safe to refresh either way.
        ...(refModelId !== null &&
        tab.kind === "subagent" &&
        !_inflightModelPatches.has(subagentId)
          ? { modelId: refModelId, modelProvider: refModelProvider }
          : {}),
        ...(tab.subagentMeta !== undefined
          ? {
              subagentMeta: {
                ...tab.subagentMeta,
                status: detail.status,
                owner: detail.owner,
                ...(typeof detail.used_tokens === "number"
                  ? { usedTokens: detail.used_tokens }
                  : {}),
                ...(typeof detail.budget_tokens === "number"
                  ? { budgetTokens: detail.budget_tokens }
                  : {}),
                ...(typeof detail.ratio === "number"
                  ? { ratio: detail.ratio }
                  : {}),
                ...(typeof detail.raw_used_tokens === "number"
                  ? { rawUsedTokens: detail.raw_used_tokens }
                  : {}),
                ...(typeof detail.raw_ratio === "number"
                  ? { rawRatio: detail.raw_ratio }
                  : {}),
                // Same SUBA-MODEL-RACE-1 guard for the nested `subagentMeta`
                // model fields: skip when a PATCH is in flight.
                ...(refModelId !== null && !_inflightModelPatches.has(subagentId)
                  ? { modelId: refModelId, modelProvider: refModelProvider }
                  : {}),
              },
            }
          : {}),
      });
      // SubAgentRail index cache — keep the per-root-conversation cache in
      // sync with the freshly-fetched detail (status / usage / model) so the
      // chip's state stays honest even after the tab is closed. Same guard as
      // above: skip the model fields when a PATCH is in flight.
      this._upsertSubAgentIndexEntry({
        subagentId: detail.subagent_id,
        rootConversationId: detail.root_conversation_id,
        parentSubagentId: detail.parent_subagent_id ?? null,
        depth: detail.depth ?? 1,
        title: tab.title.replace(/^SubAgent:\s*/i, "") || detail.subagent_id,
        status: detail.status,
        owner: detail.owner,
        ...(typeof detail.used_tokens === "number"
          ? { usedTokens: detail.used_tokens }
          : {}),
        ...(typeof detail.budget_tokens === "number"
          ? { budgetTokens: detail.budget_tokens }
          : {}),
        ...(refModelId !== null && !_inflightModelPatches.has(subagentId)
          ? { modelId: refModelId, modelProvider: refModelProvider }
          : {}),
      });
    },

    /**
     * Switch the model of an OPEN sub-agent tab (V2 enhancement).
     *
     * Persists the new model to the sub-agent's session via
     * `PATCH /api/chat/subagents/{id}` (CSRF auto-attached by `apiJson` on the
     * mutating method) and patches the tab from the AUTHORITATIVE returned
     * detail — so the budget (denominator) recomputes against the new model
     * while `usedTokens` (numerator) is untouched (the backend recomputes
     * budget from session.model_id and leaves used unchanged). This is
     * deliberately scoped to THIS sub-agent only: it does NOT write the global
     * chat-model preference and does NOT touch the parent tab — switching a
     * sub-agent's model affects only that sub-agent (State-Truth-First: the
     * session is the truth source, surfaced via the PATCH response).
     *
     * On failure the local state is left unchanged and the error propagates to
     * the caller (the composable surfaces a project toast — never window.alert,
     * AGENTS.md §3.9.2).
     */
    async setSubAgentModel(
      tabId: TabId,
      modelId: string,
      modelProvider: string,
    ): Promise<void> {
      // Only meaningful for a sub-agent tab (kind === "subagent"); the
      // composer never reaches this action for an ordinary chat tab.
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined || tab.kind !== "subagent") return;
      const subagentId = tab.subagentMeta?.subagentId;
      if (subagentId === undefined || subagentId === "") return;
      // SUBA-MODEL-RACE-1: mark this sub-agent's model PATCH as in-flight so
      // a concurrent `_refreshSubAgentTab` (e.g. from `refreshFromSnapshot`
      // at the same turn's terminal) skips writing `modelId` from its
      // possibly-stale GET — we're about to write the authoritative value
      // ourselves on PATCH success. MUST be set BEFORE the first `await`
      // so that a refresh microtask kicked off in parallel sees the marker
      // already. Cleared in the `finally` block at the function bottom.
      _inflightModelPatches.add(subagentId);
      try {
        const { apiJson } = await import("@/api");
        interface PatchBody {
          model_id: string;
          model_provider: string | null;
        }
        interface SubAgentDetail {
          subagent_id: string;
          status: string;
          owner: string;
          used_tokens?: number;
          budget_tokens?: number;
          ratio?: number;
          raw_used_tokens?: number;
          raw_ratio?: number;
          model_id?: string | null;
          model_provider?: string | null;
        }
        const detail = await apiJson<SubAgentDetail, PatchBody>(
          "PATCH",
          `/api/chat/subagents/${encodeURIComponent(subagentId)}`,
          {
            model_id: modelId,
            // Send the provider when known (backfills the session's null provider);
            // null tells the backend to leave it unset rather than blank it.
            model_provider: modelProvider !== "" ? modelProvider : null,
          },
        );
        // State-Truth-First: patch from the RETURNED detail (the backend
        // recomputed budget against the new model; used is unchanged). Prefer the
        // server's echoed model_id/provider; fall back to the requested values.
        const newModelId =
          typeof detail.model_id === "string" && detail.model_id !== ""
            ? detail.model_id
            : modelId;
        const newModelProvider =
          typeof detail.model_provider === "string"
            ? detail.model_provider
            : modelProvider;
        const cur = this.tabs.find((t) => t.id === tabId);
        if (cur === undefined || cur.kind !== "subagent" || cur.subagentMeta === undefined) return;
        this._patchTab(tabId, {
          // Drive the ModelDropdown's selected item from the session truth.
          modelId: newModelId,
          modelProvider: newModelProvider,
          subagentMeta: {
            ...cur.subagentMeta,
            modelId: newModelId,
            modelProvider: newModelProvider,
            // Budget (denominator) recomputed by the backend against the new
            // model; `usedTokens` (numerator) is intentionally NOT touched.
            ...(typeof detail.budget_tokens === "number"
              ? { budgetTokens: detail.budget_tokens }
              : {}),
            ...(typeof detail.ratio === "number" ? { ratio: detail.ratio } : {}),
            ...(typeof detail.raw_ratio === "number"
              ? { rawRatio: detail.raw_ratio }
              : {}),
          },
        });
      } finally {
        // SUBA-MODEL-RACE-1: always clear the in-flight marker, even on error.
        _inflightModelPatches.delete(subagentId);
      }
    },

    /**
     * Append a slash-command echo / reply message (V1 `_addCommandMsg`
     * / `_addCommandReply`, useChat.js:1457-1476).
     *
     * Unlike `pushUserMessage` this does NOT touch the streaming state
     * machine — command messages are pure display artifacts. They carry
     * `isCommandMsg` / `isCommandReply` so they are excluded from any
     * model-send / persistence path (the transport forwards prompt
     * strings only, and history reload never returns them). Works in
     * any tab status, including `streaming` (e.g. `/stop`).
     */
    appendCommandMessage(
      tabId: TabId,
      input: {
        role: ChatMessageRole;
        content: string;
        isCommandMsg?: boolean;
        isCommandReply?: boolean;
      },
    ): string | null {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return null;
      }
      const id = nextMessageId();
      const msg: ChatMessage = {
        id,
        role: input.role,
        content: input.content,
        createdAt: Date.now(),
        ...(tab.conversationId !== null
          ? { conversationId: tab.conversationId }
          : {}),
        ...(input.isCommandMsg === true ? { isCommandMsg: true } : {}),
        ...(input.isCommandReply === true ? { isCommandReply: true } : {}),
      };
      this._patchTab(tabId, { messages: [...tab.messages, msg] });
      return id;
    },

    /**
     * Mark a specific message as send-failed (V1 index.html:685-689).
     * Used by the transport layer to associate a turn failure with the
     * user message that triggered it, so ChatMessageList can render a
     * per-message error banner + ↻ Retry button. No-op if the message
     * id is not found.
     */
    setMessageSendError(
      tabId: TabId,
      messageId: string,
      error: string,
      code: string | null = null,
    ): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      const idx = tab.messages.findIndex((m) => m.id === messageId);
      if (idx < 0) {
        return;
      }
      const target = tab.messages[idx];
      if (target === undefined) {
        return;
      }
      const messages = [
        ...tab.messages.slice(0, idx),
        { ...target, sendError: error, sendErrorCode: code },
        ...tab.messages.slice(idx + 1),
      ];
      this._patchTab(tabId, { messages });
    },

    /** Clear the send-failed marker on a message (called on retry). */
    clearMessageSendError(tabId: TabId, messageId: string): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      const idx = tab.messages.findIndex((m) => m.id === messageId);
      if (idx < 0) {
        return;
      }
      const target = tab.messages[idx];
      if (target === undefined || target.sendError === null || target.sendError === undefined) {
        return;
      }
      const messages = [
        ...tab.messages.slice(0, idx),
        { ...target, sendError: null, sendErrorCode: null },
        ...tab.messages.slice(idx + 1),
      ];
      this._patchTab(tabId, { messages });
    },

    /**
     * Remove a single message by id (used by the retry flow, which
     * removes the failed user message before re-sending its content —
     * V1 `retryLastMessage` parity, useChat.js:2800-2807). Resets the
     * tab to `idle` so a fresh turn can start (a failed turn leaves the
     * tab in `error`). No-op if the message id is not found.
     */
    removeMessage(tabId: TabId, messageId: string): void {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      const exists = tab.messages.some((m) => m.id === messageId);
      if (!exists) {
        return;
      }
      this._patchTab(tabId, {
        messages: tab.messages.filter((m) => m.id !== messageId),
        status: "idle",
        lastError: null,
      });
    },

    /**
     * Load the *newest* page of historical messages for a tab from the
     * backend (`GET /api/chat/conversations/{id}/messages`).
     *
     * V1 parity (useChat.js:795-822): the first load shows the most
     * recent `HISTORY_PAGE_SIZE` messages; older pages are pulled in on
     * scroll-up via `loadMoreMessages` and prepended.
     *
     * V2 backend specifics (TestClient-verified, see PR manifest):
     *   - `GET …/messages?cursor=position:<int>&limit=<n>` returns a
     *     FORWARD, ascending page starting at `position`.
     *   - `cursor=null` ⇒ oldest page (position 0).
     *   - `next_cursor="position:<lastPos+1>"` points at the next *newer*
     *     page (null when forward-exhausted).
     *   - `GET …/conversations/{id}` carries `message_count`.
     * To emulate V1's newest-first view we read `message_count`, compute
     * `startPos = max(0, count - PAGE_SIZE)` and fetch from there. Older
     * pages decrement `startPos` (see `loadMoreMessages`).
     *
     * Resilient to failures: on any error the tab.messages stays as-is;
     * no toast is fired (the sidebar surfaces a banner separately).
     */
    async loadHistoryMessages(tabId: TabId): Promise<void> {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      const convId = tab.conversationId;
      if (convId === null || convId === "") {
        return;
      }
      // Avoid reloading if we already have any messages — the caller is
      // expected to call this once at openTab time.
      if (tab.messages.length > 0) {
        return;
      }
      // V1 index.html:407-419 parity — signal loading so the UI shows
      // skeleton cards while history is fetched.
      this._patchTab(tabId, { loadingHistory: true });
      // Lazy import to keep the store decoupled from the API barrel
      // for SSR / test isolation.
      const { apiJson } = await import("@/api");
      try {
        const { messages, startPos, detectedModel } = await fetchNewestPage(
          apiJson,
          convId,
          HISTORY_PAGE_SIZE,
        );
        // Only patch if the tab still exists and is still empty (the
        // user may have already started typing; never overwrite live
        // messages).
        const still = this.tabs.find((t) => t.id === tabId);
        if (still !== undefined && still.messages.length === 0) {
          this._patchTab(tabId, {
            messages,
            messagesOldestPos: startPos,
            // Older messages remain iff we did not start at the very top.
            hasMoreMessages: startPos > 0,
            loadingMore: false,
            loadingHistory: false,
            // Promote-ready detection seeded from the conversation summary
            // (migration 057): opening a conversation surfaces the CTA with
            // ZERO on-open disk scans. null when never detected (legacy).
            detectedModel,
            // Rehydrate the top TaskListBar from the newest todowrite call in
            // history — the bar reads `tab.todoList`, which is otherwise only
            // filled live (so a reload would hide the pill even though the
            // in-conversation snapshot cards still render). Empty when the
            // conversation has no todowrite call.
            todoList: extractLatestTodoList(messages),
          });
        } else {
          this._patchTab(tabId, { loadingHistory: false });
        }
      } catch (err) {
        this._patchTab(tabId, { loadingHistory: false });
        // State-Truth-First (AGENTS.md §🔴 铁律 1/4): the tab's persisted
        // `conversationId` (from localStorage `tabsLayout`) can point at a
        // conversation that no longer exists in the backend DB — e.g. a fresh
        // machine that kept the browser/Electron localStorage but did NOT copy
        // `/data` (empty DB). Leaving the dead id bound makes the next send go
        // straight to the stream route with a non-existent id → hard
        // `chat.conversation_not_found` with no self-heal. Detect a GENUINE
        // not-found (404 / `chat.conversation_not_found`) and reset the tab to
        // a blank conversation so the next send auto-creates a fresh one (V1
        // parity: sending is never dead-ended by a stale id). A transient
        // network/5xx error must NOT drop a valid id — only reset on not_found.
        if (isConversationNotFound(err)) {
          this._patchTab(tabId, { conversationId: null });
        }
        // Otherwise silent — sidebar already surfaces history-load failures.
        return;
      }
      // Multi-Agent discussion (block-5) — re-hydrate the conversation's
      // persisted discussion config + named-participant roster from the
      // backend (State-Truth-First: the backend is the single source of
      // truth; `tab.discussion` is NOT persisted to localStorage, only the
      // skeleton is — see `persistedLayout`). Without this, a restored /
      // reopened tab always starts with `{...DEFAULT_DISCUSSION_CONFIG,
      // participants: []}` (openTab) and the user's configured speakers
      // vanish on refresh. Reached by ALL three conversation-entry paths
      // (refresh restore / sidebar click / slash-command open) because they
      // all funnel through `loadHistoryMessages`.
      //
      // Idempotent / non-clobbering: this runs in the same once-per-load
      // path as the messages fetch (the `messages.length > 0` guard above
      // already returned for an already-loaded tab), so it never overwrites
      // a discussion config the user just edited in the panel. Graceful:
      // a failed discussion fetch is swallowed — it must NEVER block or
      // throw out of the (primary) messages load; `tab.discussion` then
      // stays at its default (current behaviour, no regression).
      try {
        const { useDiscussionStore } = await import("@/stores/discussion");
        const ds = useDiscussionStore();
        const cfg = await ds.fetchConfig(convId);
        // Only write back if the tab still exists and still points at the
        // same conversation (the user may have switched/closed it during
        // the round-trip) — never leak one conversation's roster onto
        // another.
        const cur = this.tabs.find((t) => t.id === tabId);
        if (cur !== undefined && cur.conversationId === convId) {
          this.setDiscussion(tabId, cfg);
          // Now that the participant roster is re-hydrated, resolve each
          // history assistant message's `senderId` (persisted on the row, set
          // by historyMapper) into its speaker display name + avatar colour.
          // The live stream gets these from the `speaker_changed` frame; a
          // reloaded history bubble has only the id and must look them up here.
          // Idempotent: only fills messages that have a senderId but no
          // senderName yet, so re-runs / live messages are never clobbered.
          //
          // IMPORTANT (2026-06-22 fix): mutate ONLY the affected messages
          // in place — do NOT replace the whole `tab.messages` array. An
          // array-reference swap here (the previous `t2.messages = map(...)`)
          // ran in a SECOND async tick (after the first-paint patch), forcing
          // ChatMessageList to re-render every bubble while the chat-view flex
          // layout was still settling; in one frame the `flex:1` message pane
          // squeezed the (then non-flex-shrink:0) `.input-area` out of view —
          // the composer "disappeared" until a tab switch forced a clean
          // relayout. In-place per-message mutation keeps the array identity
          // stable, so only the handful of speaker bubbles re-render and the
          // composer layout is never disturbed.
          if (cfg.participants.length > 0) {
            const t2 = this.tabs.find((t) => t.id === tabId);
            if (t2 !== undefined && t2.conversationId === convId) {
              // Replace ONLY the affected message slots in place (splice keeps
              // the array identity stable, unlike `messages = messages.map()`).
              // `ChatMessage` fields are readonly, so we build a new object per
              // affected message and swap it into its slot — Vue re-renders just
              // that bubble (its `:key="msg.id"` is unchanged), not the whole
              // list, so the chat-view layout is never disturbed (the composer
              // stays put on first paint).
              for (let i = 0; i < t2.messages.length; i++) {
                const msg = t2.messages[i];
                if (
                  msg === undefined ||
                  msg.role !== "assistant" ||
                  msg.senderId === undefined ||
                  msg.senderId === null ||
                  msg.senderId === "" ||
                  (msg.senderName !== undefined && msg.senderName !== null)
                ) {
                  continue;
                }
                const name = resolveSpeakerName(t2, msg.senderId);
                if (name === null) continue;
                t2.messages.splice(i, 1, {
                  ...msg,
                  senderName: name,
                  senderColor: resolveSpeakerColor(t2, msg.senderId),
                });
              }
            }
          }
        }
      } catch {
        // Non-fatal — discussion config is supplementary to the message
        // history. Leave `tab.discussion` at its default on failure.
      }
      // SubAgentRail data source — fetch the FULL list of sub-agents rooted at
      // this main-agent conversation (including sub-agents whose tab is not
      // currently open) so the rail's chip list is complete. Fire-and-forget:
      // non-fatal on failure (rail falls back to filtering `state.tabs`).
      // Only for MAIN-agent tabs (`kind !== "subagent"`); a sub-agent tab
      // shares its parent's rootConversationId but the parent's own load
      // already covered it.
      const mainTab = this.tabs.find((t) => t.id === tabId);
      if (mainTab !== undefined && mainTab.kind !== "subagent") {
        void this._fetchSubAgentIndex(convId);
      }
    },

    /**
     * Load one older page and PREPEND it (V1 useChat.js:869-902).
     *
     * Called by the IntersectionObserver sentinel in ChatMessageList when
     * the user scrolls to the top. V2 has no native "load older" cursor,
     * so we page *backwards* by computing the previous block's start
     * position from `messagesOldestPos`:
     *   newStart = max(0, messagesOldestPos - PAGE_SIZE)
     *   limit    = messagesOldestPos - newStart   (exact older slice)
     *
     * Re-entrancy guarded by `loadingMore` (V1 `isLoadingMoreMessages`).
     * The view layer is responsible for preserving scroll position after
     * the prepend (V1 useChat.js:879-890); the store only mutates data.
     */
    async loadMoreMessages(tabId: TabId): Promise<void> {
      const tab = this.tabs.find((t) => t.id === tabId);
      if (tab === undefined) {
        return;
      }
      if (
        !tab.hasMoreMessages ||
        tab.loadingMore ||
        tab.messagesOldestPos <= 0
      ) {
        return;
      }
      const convId = tab.conversationId;
      if (convId === null || convId === "") {
        return;
      }
      this._patchTab(tabId, { loadingMore: true });
      const { apiJson } = await import("@/api");
      const oldestPos = tab.messagesOldestPos;
      try {
        const { messages: older, newStart } = await fetchOlderPage(
          apiJson,
          convId,
          oldestPos,
          HISTORY_PAGE_SIZE,
        );
        const still = this.tabs.find((t) => t.id === tabId);
        if (still === undefined) {
          return;
        }
        if (older.length > 0) {
          this._patchTab(tabId, {
            messages: [...older, ...still.messages],
            messagesOldestPos: newStart,
            hasMoreMessages: newStart > 0,
            loadingMore: false,
          });
        } else {
          // Defensive: nothing came back — stop the observer.
          this._patchTab(tabId, {
            hasMoreMessages: false,
            loadingMore: false,
          });
        }
      } catch {
        // Keep current state; just release the in-flight guard so a
        // later scroll can retry (V1 logs + resets the flag).
        this._patchTab(tabId, { loadingMore: false });
      }
    },

    clearMessages(tabId: TabId): void {
      this._patchTab(tabId, {
        messages: [],
        streamingContent: "",
        streamingToolCalls: [],
        activeToolMessageId: null,
        roundMessageIds: {},
        roundSubAgentMessageIds: {},
        activeSubAgentMessageId: null,
        streamingUsage: null,
        streamingPerf: null,
        streamingSubAgentBlocks: [],
        // Must match the reset set of setStreaming/confirmDone/confirmAbort/
        // recordError: clearing mid-turn (e.g. "Clear conversation" while an
        // `end` frame already stamped streamingRequestId) must NOT leave a
        // stale request_id that the next confirmDone would stamp onto an
        // unrelated message → a phantom 📄 button / wrong prompt snapshot
        // (State-Truth-First: the request_id must reflect the live turn).
        streamingRequestId: null,
      });
    },

    reset(): void {
      _resetChatTabsTransient();
      this.tabs = [];
      this.activeTabId = null;
    },

    // ---------------------------------------------------------------
    // Internal helpers
    // ---------------------------------------------------------------

    _touch(tabId: TabId): void {
      this._patchTab(tabId, { lastActiveAt: Date.now() });
    },

    _patchTab(tabId: TabId, patch: Partial<ChatTab>): void {
      const idx = this.tabs.findIndex((t) => t.id === tabId);
      if (idx < 0) {
        return;
      }
      const target = this.tabs[idx];
      if (target === undefined) {
        return;
      }
      // Perf (multi-agent main-thread freeze fix): mutate the matched tab
      // IN PLACE instead of replacing the whole `this.tabs` array.
      //
      // The previous `this.tabs = [...slice, {...target, ...patch}, ...slice]`
      // swapped the array's identity on EVERY patch. During agentic streaming
      // each active tab calls `_patchTab({ streamingContent })` once per
      // animation frame (see `_flushStreamingBuffer` ~L881), plus a synchronous
      // patch on every non-chunk frame. Because `ChatTabStrip.vue:281` renders
      // the tab bar with `v-for="tab in tabs.tabs.value"` over that same array,
      // a new array reference forced the ENTIRE tab-strip v-for to re-diff every
      // frame (recomputing `visibleTitle(tab)`, `tab.status`, all class bindings
      // for every tab). With N concurrent streams that is O(N²) work per frame —
      // the main thread saturates and the browser reports "page unresponsive"
      // (and worse the more agents run; stops the instant streams stop).
      //
      // `Object.assign(target, patch)` writes the patched fields directly onto
      // the existing reactive tab object: the array reference is unchanged, so
      // the tab strip's `v-for` does NOT re-diff, while Vue's reactivity still
      // tracks the per-field property writes (incl. wholesale array fields like
      // `messages`/`streamingToolCalls` that the callers already replace as new
      // arrays). Behaviour is identical to the spread (same fields set to the
      // same values, including explicit `undefined`/`null` resets — no caller
      // relies on the array's identity changing; the only `watch`/`computed`
      // observers, `ChatTabStrip.vue:207` and `ChatView.vue:333`, key off
      // derived per-field values, not the array reference).
      Object.assign(target, patch);
    },
  },
});
