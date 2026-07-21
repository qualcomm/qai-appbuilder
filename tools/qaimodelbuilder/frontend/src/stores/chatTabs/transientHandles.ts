// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Per-tab transient handles for the chat store (cohesion split, ARCH-1).
 *
 * Moved verbatim from `chatTabs.ts`. These handles are deliberately NOT
 * reactive Pinia state — they live in module-level `Map`s keyed by tab id.
 * This sidesteps both (a) Vue reactivity wrapping native objects whose
 * identity must be stable, and (b) the historical Pinia bug around
 * `WebSocket` constructors. Each tab's controller is strictly isolated;
 * cancelling one is invisible to the others (refactor-plan §10.6 invariant 2).
 *
 * The functions are re-exported from `@/stores/chatTabs` so existing
 * `import { getOrCreateAbortController, … } from "@/stores/chatTabs"`
 * call sites (transport / appBuilder / tests) keep working unchanged.
 */
import type { TabId } from "../_chatTabsTypes";
import { BoundedOutputBuffer } from "./toolOutputPreview";

const _abortControllers = new Map<TabId, AbortController>();
const _webSockets = new Map<TabId, WebSocket>();

// ---------------------------------------------------------------------------
// Sub-agent live-stream subscriptions (block 2).
//
// A sub-agent tab subscribes to `GET /api/chat/subagents/{id}/stream` (SSE) so
// it shows the sub-agent's progress LIVE — independent of the parent
// conversation's stream. The subscription is driven by a fetch+AbortController
// (apiSSE), so it is a transient native handle (same rationale as the maps
// above): non-reactive, aborted on tab close so the SSE connection is released
// and the consumer loop exits. Re-opening the same sub-agent reconnects and
// backfills the missed frames from the broadcaster (cursor replay).
//
// ── Composite (tabId, subagentId) keying ──────────────────────────────────
//
// A single top-level tab can host MULTIPLE concurrent sub-agent SSE streams
// in parallel (e.g. an orchestrator main-agent spawning several sub-agents
// whose progress panels stream live independent of the parent's own
// transcript stream). We therefore key these maps by the composite
// `(tabId, subagentId)` instead of the bare tab id — otherwise a second
// concurrent sub-agent under the same tab would abort the first.
//
// Physical layout: nested `Map<TabId, Map<subagentId, T>>`. The outer map
// gives O(1) "all sub-agents of this tab" lookup (used by the legacy
// single-argument bridge below); the inner map gives O(1) "this exact
// sub-agent" lookup. New ref-aware callers pass an explicit `subagentId`
// (distinct inner entry); legacy single-arg callers store under a sentinel
// inner key (`LEGACY_SUBAGENT_KEY`) so their behaviour is byte-equivalent
// to a single-layer `Map<TabId, T>` (one entry per tab id).
//
// Legacy single-arg operations act on "all entries for this tab" so
// `closeTab(tabId)` → `clearSubAgentStream(tabId)` still tears down every
// sub-agent stream that tab owns in one shot — required by the bridge
// callers that don't yet thread a `subagentId` through.
const LEGACY_SUBAGENT_KEY = "__legacy__";

const _subAgentStreamControllers = new Map<
  TabId,
  Map<string, AbortController>
>();

// ---------------------------------------------------------------------------
// Sub-agent WS "last applied sequence" tracker (block 2 — from_seq).
//
// Keyed by `(tabId, subagentId)`: the highest broadcaster sequence this
// (tab, sub-agent) has ever applied to `tab.messages` through `applyFrame`.
// Persisted across:
//   * WS close / re-subscribe (tab switch, layout restore, take-over, WS
//     drop + reconnect) — so the next connect passes `from_seq = lastSeq + 1`
//     and the server elides frames the tab already rendered.
//   * `SubAgentStreamBroadcaster.register` replacing an entry (resume of the
//     same sub-agent for another run) — sequence numbers are inherited by
//     the new entry (see broadcaster docstring), so lastSeq stays correct
//     across runs without any client-side reset.
// Cleared ONLY when:
//   * `clearSubAgentLastSeq(tabId)` — the tab closes (all its sub-agent
//     slots released; see chatTabs.closeTab teardown).
//   * `clearSubAgentLastSeq(tabId, subagentId)` — this specific sub-agent's
//     tracker is explicitly forgotten (e.g. a hard reload of the tab from
//     the authoritative HTTP snapshot, where the store rebuilds messages
//     from scratch and the WS is about to re-subscribe as if new).
//
// This tracker is the ONLY piece of client state that participates in the
// broadcaster's `from_seq` protocol; no per-frame dedupe layer exists —
// the server simply does not resend what the client has already applied.
// Module-level `Map` (non-reactive), same rationale as the other transient
// handles above.
const _subAgentLastAppliedSeq = new Map<TabId, Map<string, number>>();

/** Read the highest sequence this (tabId, subagentId) has applied. Returns
 *  `-1` when no frame has ever been applied — the caller sends
 *  ``from_seq = 0`` in that case (a fresh subscribe replays from the top). */
export function getSubAgentLastAppliedSeq(
  tabId: TabId,
  subagentId: string,
): number {
  return _subAgentLastAppliedSeq.get(tabId)?.get(subagentId) ?? -1;
}

/** Record that a frame with `sequence` was successfully applied. Monotonic
 *  (no-op if `sequence <= current`). Called by `_subscribeSubAgentStream`
 *  after `applyFrame` for that frame returns. */
export function recordSubAgentAppliedSeq(
  tabId: TabId,
  subagentId: string,
  sequence: number,
): void {
  if (!Number.isFinite(sequence) || sequence < 0) return;
  let inner = _subAgentLastAppliedSeq.get(tabId);
  if (inner === undefined) {
    inner = new Map();
    _subAgentLastAppliedSeq.set(tabId, inner);
  }
  const current = inner.get(subagentId) ?? -1;
  if (sequence > current) {
    inner.set(subagentId, sequence);
  }
}

/** Forget the (tabId, subagentId)'s lastSeq — or all sub-agents in `tabId`
 *  when `subagentId` is omitted. Used on tab close (drop everything) and by
 *  callers that explicitly rebuild messages from the HTTP snapshot (they
 *  want the next WS subscribe to replay from the top). */
export function clearSubAgentLastSeq(
  tabId: TabId,
  subagentId?: string,
): void {
  if (subagentId === undefined) {
    _subAgentLastAppliedSeq.delete(tabId);
    return;
  }
  const inner = _subAgentLastAppliedSeq.get(tabId);
  if (inner === undefined) return;
  inner.delete(subagentId);
  if (inner.size === 0) _subAgentLastAppliedSeq.delete(tabId);
}

/** Test/reset helper — drops every tab's tracker. Called by
 *  `_resetChatTabsTransient` so specs start from a clean slate. */
export function _resetSubAgentLastAppliedSeq(): void {
  _subAgentLastAppliedSeq.clear();
}


// ---------------------------------------------------------------------------
// Streaming-chunk coalescing buffer (perf: stop per-token O(n²) re-render).
//
// V1 parity (useChat.js:446-463 + 1069-1097): V1 coalesces high-frequency
// streaming deltas to AT MOST one UI update per animation frame via
// requestAnimationFrame, with an explicit `_pendingText` buffer. Its comments
// say this caps Vue updates to ≤60/s "no matter how fast the backend streams"
// and avoids the layout thrash that otherwise makes long answers crawl.
//
// V2 had dropped that layer: every CHUNK frame wrote `streamingContent`
// synchronously, and the template re-runs `renderMarkdown(streamingContent)`
// (marked + highlight.js + DOMPurify over the WHOLE accumulated text) on every
// write — so a single turn's render cost grew O(n²) in the generated length.
// In the model-build tool loop (long, multi-round answers) this surfaced as
// "fast at first, then crawling".
//
// These buffers are per-tab, non-reactive (module-level Maps, same rationale
// as the handles above). The store appends raw chunks here and schedules a
// rAF flush; the flush writes the coalesced text into reactive
// `streamingContent` ONCE per frame. Terminal transitions (done/abort/error)
// flush synchronously first so no trailing text is lost.
const _streamBuffers = new Map<TabId, string>();

// ── Tiered flush scheduling (perf: relieve main thread under concurrent
//    streaming sessions) ──────────────────────────────────────────────────
//
// The active (foreground) tab keeps the original per-animation-frame flush so
// its streaming stays glassy-smooth (≤~60 reactive writes/s, unchanged). But a
// BACKGROUND streaming tab (one of several concurrent agentic sessions the user
// is NOT looking at) does not need a reactive write + dependent re-render every
// frame — coalescing its chunks far more coarsely cuts the main-thread cost of
// fan-out streaming without the user ever noticing (they're not watching it).
// When the page itself is hidden (tab/window in background), we coalesce even
// more coarsely.
//
// These thresholds only govern the AUTOMATIC next flush. Every synchronous
// flush path (`flushStreamingNow` / `flushRoundChunkNow` / terminal transitions
// / the pre-non-chunk-frame flush in `applyFrame`) cancels the pending schedule
// and flushes immediately regardless of tier, so tool_call/end/done ordering is
// never affected and no buffer is ever left behind.
/** Background (non-active) streaming tab: coalesce ~every 150ms (100–300ms
 *  budget). The user is not watching it, so per-frame writes are wasteful. */
const BACKGROUND_FLUSH_MS = 150;
/** Page hidden (whole tab/window backgrounded): coalesce ~every 500ms. */
const HIDDEN_FLUSH_MS = 500;
/** A standalone sub-agent tab that is being actively watched LIVE (the user
 *  opened it to follow a sub-agent's progress) but is NOT the foreground tab —
 *  e.g. resume re-subscribes its WS while the user looks at the parent
 *  conversation. Such a tab must stream visibly (not塌缩成一次刷新), but it does
 *  NOT need per-frame rAF writes (the frames arrive same-process with zero
 *  network delay → a rAF would塌缩 them anyway, and a per-frame SYNC write would
 *  replace the whole `messages` array每帧 → O(n²) 全列表重渲染). A ~50ms (20fps)
 *  middle tier gives真流式 observable progress while封顶刷新频率. Page-hidden
 *  still downshifts to `HIDDEN_FLUSH_MS` (the user can't see it at all). */
const SUBAGENT_LIVE_FLUSH_MS = 50;

/** Tabs currently driving a LIVE sub-agent WS subscription (block 2). Membership
 *  is owned by `_subscribeSubAgentStream` (added on first frame /
 *  `ensureStreaming`, removed on terminal / abort / close). When a tab is in
 *  this set AND is not the foreground tab AND the page is visible, its streaming
 *  flush uses `SUBAGENT_LIVE_FLUSH_MS` instead of the coarse background tier — so
 *  a watched-but-background sub-agent tab streams at ~20fps rather than塌缩 into a
 *  single paint. Module-level (non-reactive), same rationale as the handle maps
 *  above.
 *
 *  ── Composite (tabId, subagentId) keying ────────────────────────────────
 *  Same rationale as `_subAgentStreamControllers` above: one tab id can host
 *  multiple parallel sub-agent streams concurrently, so the LIVE marker is
 *  keyed by `(tabId, subagentId)`. Physical layout: `Map<TabId, Set<string>>`
 *  — the outer map keys by tab so the single-arg `isSubAgentTabLive(tabId)` can
 *  answer "ANY sub-agent of this tab live?" in O(1), and so `closeTab` can
 *  wipe every sub-agent marker the tab owned in one shot. Single-arg legacy
 *  callers store under `LEGACY_SUBAGENT_KEY` → byte-equivalent to a single-
 *  layer `Set<TabId>` semantics. */
const _liveSubAgentTabs = new Map<TabId, Set<string>>();

/** Internal helper: get-or-create the inner controllers map for a tab. */
function controllerInner(tabId: TabId): Map<string, AbortController> {
  let inner = _subAgentStreamControllers.get(tabId);
  if (inner === undefined) {
    inner = new Map();
    _subAgentStreamControllers.set(tabId, inner);
  }
  return inner;
}

/** Internal helper: get-or-create the inner live-set for a tab. */
function liveInner(tabId: TabId): Set<string> {
  let inner = _liveSubAgentTabs.get(tabId);
  if (inner === undefined) {
    inner = new Set();
    _liveSubAgentTabs.set(tabId, inner);
  }
  return inner;
}

/** Mark a tab as driving a live sub-agent stream (raises its background flush
 *  tier to `SUBAGENT_LIVE_FLUSH_MS`). Idempotent.
 *
 *  When `subagentId` is given, marks the specific `(tabId, subagentId)`
 *  pair. When omitted (legacy single-arg bridge), marks the tab under the
 *  legacy sentinel — `isSubAgentTabLive(tabId)` (no `subagentId`) returns
 *  true iff at least one inner entry exists, so single-arg behaviour is
 *  byte-equivalent to a single-layer `Set<TabId>` semantics. */
export function markSubAgentTabLive(tabId: TabId, subagentId?: string): void {
  liveInner(tabId).add(subagentId ?? LEGACY_SUBAGENT_KEY);
}

/** Clear a tab's live sub-agent marker (it returns to the normal background
 *  tier). Called on terminal / abort / tab close. Idempotent.
 *
 *  When `subagentId` is given, removes ONLY that specific
 *  `(tabId, subagentId)` pair (and prunes the empty inner map). When omitted
 *  (legacy single-arg bridge), removes ALL markers under the tab — equivalent
 *  to a single-layer `Set<TabId>.delete(tabId)`. */
export function unmarkSubAgentTabLive(
  tabId: TabId,
  subagentId?: string,
): void {
  const inner = _liveSubAgentTabs.get(tabId);
  if (inner === undefined) return;
  if (subagentId === undefined) {
    _liveSubAgentTabs.delete(tabId);
    return;
  }
  inner.delete(subagentId);
  if (inner.size === 0) {
    _liveSubAgentTabs.delete(tabId);
  }
}

/** True when `(tabId, subagentId)` is currently driving a live sub-agent
 *  stream. Single-arg call returns true iff ANY sub-agent under the tab is
 *  live (byte-equivalent to a `Set<TabId>.has(tabId)` check). Exported as
 *  the public read API; the legacy-named private `isLiveSubAgentTab` below
 *  is preserved for the scheduler's own internal callers. */
export function isSubAgentTabLive(
  tabId: TabId,
  subagentId?: string,
): boolean {
  const inner = _liveSubAgentTabs.get(tabId);
  if (inner === undefined) return false;
  if (subagentId === undefined) {
    return inner.size > 0;
  }
  return inner.has(subagentId);
}

/** Internal predicate used by the tiered scheduler. Kept private + named as
 *  before so the existing call sites (`scheduleTiered` / `desiredRank`) keep
 *  matching at the source level. Identical semantics to `isSubAgentTabLive`
 *  in its single-arg form: ANY sub-agent of the tab live ⇒ true. */
function isLiveSubAgentTab(tabId: TabId): boolean {
  return isSubAgentTabLive(tabId);
}

/** List every sub-agent currently marked live under a tab. The legacy
 *  single-arg `markSubAgentTabLive(tabId)` sentinel entries are filtered out
 *  so callers only see real sub-agent ids. Order is insertion order (Set's
 *  natural iteration order). */
export function listLiveSubAgentsInTab(tabId: TabId): string[] {
  const inner = _liveSubAgentTabs.get(tabId);
  if (inner === undefined) return [];
  const out: string[] = [];
  for (const id of inner) {
    if (id !== LEGACY_SUBAGENT_KEY) {
      out.push(id);
    }
  }
  return out;
}

/** A pending flush handle, tagged so `cancel*` uses the matching canceller
 *  (`cancelAnimationFrame` for rAF, `clearTimeout` for the downshifted timer).
 *  The timeout variant also records its `tier` so the schedulers' "already
 *  scheduled" branch can tell a live-sub-agent 50ms timer apart from a coarse
 *  150/500ms background timer (and so NOT mistakenly upgrade/downgrade it). */
type ScheduledHandle =
  | { readonly kind: "raf"; readonly id: number }
  | {
      readonly kind: "timeout";
      readonly id: number;
      readonly tier: "frame" | "subagent_live" | "background" | "hidden";
    };

/** True when the page is currently hidden (whole browser tab/window in the
 *  background). Guarded for non-DOM / test environments. */
function isPageHidden(): boolean {
  return (
    typeof document !== "undefined" &&
    document.visibilityState === "hidden"
  );
}

/**
 * Schedule `run` according to the streaming tier (highest fidelity first):
 *   - active tab + page visible → next animation frame (~16ms, unchanged).
 *   - page hidden               → coarse timer (`HIDDEN_FLUSH_MS`).
 *   - live sub-agent tab (watched but not foreground) + page visible →
 *     `SUBAGENT_LIVE_FLUSH_MS` (~20fps) so it streams visibly without per-frame
 *     全列表重渲染.
 *   - other background tab      → downshifted timer (`BACKGROUND_FLUSH_MS`).
 * Falls back to a 16ms timer for the active/visible case in non-DOM/test
 * environments where `requestAnimationFrame` is unavailable, so behaviour stays
 * deterministic under unit tests.
 */
function scheduleTiered(
  run: () => void,
  isActive: boolean,
  tabId: TabId,
): ScheduledHandle {
  if (isActive && !isPageHidden()) {
    if (typeof globalThis.requestAnimationFrame === "function") {
      return { kind: "raf", id: globalThis.requestAnimationFrame(run) };
    }
    // Non-DOM/test fallback: a 16ms timer stands in for "next frame".
    return {
      kind: "timeout",
      tier: "frame",
      id: globalThis.setTimeout(run, 16) as unknown as number,
    };
  }
  if (isPageHidden()) {
    return {
      kind: "timeout",
      tier: "hidden",
      id: globalThis.setTimeout(run, HIDDEN_FLUSH_MS) as unknown as number,
    };
  }
  // Page visible, not the foreground tab. A LIVE sub-agent tab the user opened
  // to watch streams at ~20fps; any other background tab uses the coarse tier.
  if (isLiveSubAgentTab(tabId)) {
    return {
      kind: "timeout",
      tier: "subagent_live",
      id: globalThis.setTimeout(
        run,
        SUBAGENT_LIVE_FLUSH_MS,
      ) as unknown as number,
    };
  }
  return {
    kind: "timeout",
    tier: "background",
    id: globalThis.setTimeout(run, BACKGROUND_FLUSH_MS) as unknown as number,
  };
}

/** Cancel a previously scheduled flush using the canceller matching its kind. */
function cancelScheduled(handle: ScheduledHandle): void {
  if (handle.kind === "raf") {
    if (typeof globalThis.cancelAnimationFrame === "function") {
      globalThis.cancelAnimationFrame(handle.id);
    } else {
      globalThis.clearTimeout(handle.id);
    }
  } else {
    globalThis.clearTimeout(handle.id);
  }
}

const _streamRafHandles = new Map<TabId, ScheduledHandle>();

/** Fidelity rank of a pending handle (higher = flushes sooner / smoother).
 *  Used by the schedulers' "already scheduled" branch to decide whether a NEW
 *  schedule request (with possibly different active/live/visibility tiering)
 *  should UPGRADE the pending handle. We only ever upgrade to a higher rank
 *  (e.g. a stale 150ms background timer → a 50ms live-sub-agent timer, or → a
 *  rAF when the tab became foreground); never downgrade (a foreground rAF must
 *  stay glassy even if a later chunk's `isActive` momentarily races false). */
function handleRank(handle: ScheduledHandle): number {
  if (handle.kind === "raf") return 4;
  switch (handle.tier) {
    case "frame":
      return 4;
    case "subagent_live":
      return 3;
    case "background":
      return 2;
    case "hidden":
      return 1;
  }
}

/** The fidelity rank the NEXT schedule for `tabId` would produce given the
 *  current active/live/visibility tiering (mirrors `scheduleTiered`'s branch
 *  order). Lets the "already scheduled" path upgrade-only. */
function desiredRank(tabId: TabId, isActive: boolean): number {
  if (isActive && !isPageHidden()) return 4; // rAF / frame
  if (isPageHidden()) return 1; // hidden
  if (isLiveSubAgentTab(tabId)) return 3; // sub-agent live (~20fps)
  return 2; // background
}

/** Shared "already scheduled" reconciliation for both flush schedulers: when a
 *  pending handle exists, UPGRADE it (cancel + reschedule) iff the current
 *  tiering would produce a strictly higher-fidelity handle (e.g. the first
 *  chunk landed while the tab was background/non-live, then it became
 *  foreground or got marked live). Returns `true` when it (re)scheduled, so the
 *  caller returns; `false` means "no pending handle — caller should schedule
 *  fresh". Never downgrades. */
function reconcilePendingFlush(
  handles: Map<TabId, ScheduledHandle>,
  tabId: TabId,
  run: () => void,
  isActive: boolean,
): boolean {
  const existing = handles.get(tabId);
  if (existing === undefined) return false;
  if (desiredRank(tabId, isActive) > handleRank(existing)) {
    cancelScheduled(existing);
    handles.set(tabId, scheduleTiered(run, isActive, tabId));
  }
  return true; // had a pending handle either way → coalesce / upgraded
}

// ── Per-round chunk coalescing buffer (round_index zero-inference path) ──────
//
// The bottom `_streamBuffers` above is the TURN-level buffer that feeds the
// trailing `streamingContent` summary bubble (legacy / no-round_index / the
// final no-tool answer). It is NOT per-round: it has no notion of which agentic
// round its text belongs to.
//
// When the backend stamps a CHUNK with `round_index`, the text belongs to a
// SPECIFIC round's assistant message — it must land in THAT round's `content`,
// not the shared bottom buffer (the reported "云端多轮文本累积成块沉底" bug: a
// round whose text was buffered but whose tool_call never folded it leaked all
// its text into the single bottom buffer, mixing every round's narration into
// one block below all the tool cards).
//
// This per-tab buffer coalesces a round's chunks (rAF parity, same perf budget
// as the bottom buffer) while remembering which round-message id the text is
// for. When a chunk for a DIFFERENT round arrives, the prior round's buffer is
// flushed FIRST (so each round's text stays bound to its own message), then the
// new round starts buffering. The store's flusher writes the coalesced text
// into the owning round message's `content` (not `streamingContent`).
interface RoundChunkBuffer {
  /** The round message id this buffered text appends to. */
  readonly messageId: string;
  /** Coalesced text pending flush into that message's content. */
  text: string;
}
const _roundChunkBuffers = new Map<TabId, RoundChunkBuffer>();
const _roundChunkRafHandles = new Map<TabId, ScheduledHandle>();

/** Append a streaming chunk to the per-tab coalescing buffer. */
export function bufferStreamingChunk(tabId: TabId, chunk: string): void {
  _streamBuffers.set(tabId, (_streamBuffers.get(tabId) ?? "") + chunk);
}

// ── Per-round buffer API (mirrors the bottom-buffer API above) ───────────────

/** Append a chunk to the per-round coalescing buffer targeting `messageId`.
 *  The caller (`store.appendRoundChunk`) is responsible for flushing the prior
 *  round's buffer FIRST when this chunk switches to a different round message,
 *  so this only ever appends to the current round's buffer. */
export function bufferRoundChunk(
  tabId: TabId,
  messageId: string,
  chunk: string,
): void {
  const existing = _roundChunkBuffers.get(tabId);
  _roundChunkBuffers.set(tabId, {
    messageId,
    // Defensive: if a stale buffer for a DIFFERENT round somehow survived (the
    // caller normally flushes it first), start fresh rather than cross rounds.
    text:
      existing !== undefined && existing.messageId === messageId
        ? existing.text + chunk
        : chunk,
  });
}

/** Take (and clear) the buffered round text + its target message id, or null. */
export function takeBufferedRoundChunk(
  tabId: TabId,
): { messageId: string; text: string } | null {
  const buf = _roundChunkBuffers.get(tabId);
  _roundChunkBuffers.delete(tabId);
  if (buf === undefined || buf.text === "") {
    return null;
  }
  return { messageId: buf.messageId, text: buf.text };
}

/** True when there is pending buffered round text for a tab. */
export function hasBufferedRoundChunk(tabId: TabId): boolean {
  const buf = _roundChunkBuffers.get(tabId);
  return buf !== undefined && buf.text.length > 0;
}

/** The message id the per-round buffer is currently targeting, or null. */
export function peekRoundChunkTarget(tabId: TabId): string | null {
  return _roundChunkBuffers.get(tabId)?.messageId ?? null;
}

/** Schedule a flush of the per-round buffer (tiered: active tab + visible page
 *  → next animation frame; background tab / hidden page → downshifted timer —
 *  same tiering as `scheduleStreamingFlush`). `isActive` is whether `tabId` is
 *  the currently active (foreground) tab.
 *
 *  ── Backfill (transcript-so-far) suppression ─────────────────────────────
 *  When `backfill === true` (the chunk is part of a broadcaster cursor=0
 *  replay of the round's BUFFERED frames — sub-agent / active-run attach), we
 *  intentionally DO NOT schedule a flush here. The text is still appended to
 *  the underlying coalescing buffer by the caller (`bufferRoundChunk`), it
 *  just stays there — and a later commit driver flushes it:
 *    - `applyFrame`'s synchronous pre-non-chunk flush (chatTabs.ts ~1977-1987)
 *      flushes BEFORE any non-chunk frame in the backfill burst (preserves
 *      chunk-before-tool_call ordering); so a backfill burst that contains
 *      tool_call frames produces one paint per contiguous chunk run.
 *    - The backfill→live boundary (or terminal `done` / unexpected close)
 *      issues a final `flushRoundChunkNow` that commits the trailing chunk
 *      run.
 *  Net: ≤ 1 + (#non-chunk-frames-in-burst) paints, instead of one paint per
 *  chunk frame — which eliminates the user-visible "trailing transcript
 *  replayed逐字 by typewriter" bug. Live frames are unaffected
 *  (backfill=false → normal tier scheduling). */
export function scheduleRoundChunkFlush(
  tabId: TabId,
  flush: (tabId: TabId) => void,
  isActive = true,
  backfill = false,
): void {
  if (backfill) {
    // Buffer-only: do NOT schedule. The caller's backfill→live boundary
    // (chatTabs `handleFrame`) issues a single `flushRoundChunkNow` that
    // commits the whole accumulated batch as ONE patch.
    return;
  }
  const run = (): void => {
    _roundChunkRafHandles.delete(tabId);
    flush(tabId);
  };
  // Already scheduled? Upgrade-only (cancel + reschedule) when the current
  // tiering would yield a higher-fidelity handle — e.g. the first chunk landed
  // while the tab was background/non-live (a common race when a sub-agent tab's
  // first frame is processed before `activeTabId` settles, or before
  // `markSubAgentTabLive` ran), and the tab is NOW foreground or marked live.
  // Never downgrades a rAF/live timer to something coarser. See
  // `reconcilePendingFlush`. Otherwise coalesce into the pending flush.
  if (reconcilePendingFlush(_roundChunkRafHandles, tabId, run, isActive)) {
    return;
  }
  _roundChunkRafHandles.set(tabId, scheduleTiered(run, isActive, tabId));
}

/** Cancel any pending per-round flush for a tab. */
export function cancelRoundChunkFlush(tabId: TabId): void {
  const handle = _roundChunkRafHandles.get(tabId);
  if (handle !== undefined) {
    cancelScheduled(handle);
    _roundChunkRafHandles.delete(tabId);
  }
}

// ── Tool-card streaming-output coalescing + bounded preview (perf) ───────────
//
// A streaming-capable tool (exec) emits many `partial=true` `tool_result`
// frames carrying a `delta` chunk. The old reducer did `output = output +
// delta` per frame and replaced the whole `messages` array each time → O(n)
// concat + O(n) re-render per frame → O(n²) over a huge stream, plus the card
// held the entire (multi-MB) output. Two layers fix this:
//
//   1) BOUNDED PREVIEW — each card's live output is accumulated in a
//      `BoundedOutputBuffer` (frozen head + rolling tail; middle folded). The
//      card never holds more than HEAD+TAIL+marker chars regardless of stream
//      size, and each append is O(delta), not O(total).
//   2) RENDER THROTTLE — deltas are absorbed into the (non-reactive) buffer on
//      every frame, but the reactive `output` write (which replaces the
//      messages array) is coalesced to AT MOST one per animation frame (tiered,
//      same scheduler as the chunk buffers). No data is lost: the buffer keeps
//      absorbing; only the paint is throttled. Terminal frames flush
//      synchronously so the settled card never misses its last bytes.
//
// Buffers are keyed per tab → per CARD (callId ?? originating frame id) so
// parallel same-named tools (two `exec` in one round) keep separate previews.
// Module-level + non-reactive, same rationale as the chunk buffers above.
interface ToolOutputBuffer {
  readonly buffer: BoundedOutputBuffer;
  /** Whether a flush is pending for this card (it absorbed deltas since the
   *  last reactive write). */
  dirty: boolean;
}
/** tabId → (cardKey → bounded buffer). */
const _toolOutputBuffers = new Map<TabId, Map<string, ToolOutputBuffer>>();
const _toolOutputRafHandles = new Map<TabId, ScheduledHandle>();

function toolBufferMap(tabId: TabId): Map<string, ToolOutputBuffer> {
  let m = _toolOutputBuffers.get(tabId);
  if (m === undefined) {
    m = new Map();
    _toolOutputBuffers.set(tabId, m);
  }
  return m;
}

/** Absorb a streaming delta into the card's bounded preview buffer (creating it
 *  on first delta). Returns the rendered bounded preview AFTER appending, so a
 *  freshly-seeded card can store the initial preview immediately. Non-reactive:
 *  this never touches Pinia — the caller schedules a throttled flush. */
export function bufferToolOutputDelta(
  tabId: TabId,
  cardKey: string,
  delta: string,
): string {
  const m = toolBufferMap(tabId);
  let entry = m.get(cardKey);
  if (entry === undefined) {
    entry = { buffer: new BoundedOutputBuffer(), dirty: false };
    m.set(cardKey, entry);
  }
  entry.buffer.append(delta);
  entry.dirty = true;
  return entry.buffer.render();
}

/** Snapshot the current bounded preview for a card without mutating it (used by
 *  the throttled flush to write the latest preview into reactive state). Returns
 *  null when the card has no pending (dirty) buffer. Clears the dirty flag. */
export function takeDirtyToolOutput(
  tabId: TabId,
  cardKey: string,
): string | null {
  const entry = _toolOutputBuffers.get(tabId)?.get(cardKey);
  if (entry === undefined || !entry.dirty) return null;
  entry.dirty = false;
  return entry.buffer.render();
}

/** All card keys for a tab that have absorbed deltas since their last flush. */
export function dirtyToolOutputKeys(tabId: TabId): string[] {
  const m = _toolOutputBuffers.get(tabId);
  if (m === undefined) return [];
  const keys: string[] = [];
  for (const [key, entry] of m) {
    if (entry.dirty) keys.push(key);
  }
  return keys;
}

/** Drop a card's buffer once its stream settles (terminal frame) so a reused
 *  card key in a later turn starts fresh. */
export function clearToolOutputBuffer(tabId: TabId, cardKey: string): void {
  _toolOutputBuffers.get(tabId)?.delete(cardKey);
}

/** Schedule a throttled flush of a tab's dirty tool-card previews (tiered, same
 *  fidelity rules as the chunk schedulers). `isActive` = whether `tabId` is the
 *  foreground tab. Coalesces into any pending flush (upgrade-only). */
export function scheduleToolOutputFlush(
  tabId: TabId,
  flush: (tabId: TabId) => void,
  isActive = true,
): void {
  const run = (): void => {
    _toolOutputRafHandles.delete(tabId);
    flush(tabId);
  };
  if (reconcilePendingFlush(_toolOutputRafHandles, tabId, run, isActive)) {
    return;
  }
  _toolOutputRafHandles.set(tabId, scheduleTiered(run, isActive, tabId));
}

/** Cancel any pending tool-output flush for a tab (terminal / teardown). */
export function cancelToolOutputFlush(tabId: TabId): void {
  const handle = _toolOutputRafHandles.get(tabId);
  if (handle !== undefined) {
    cancelScheduled(handle);
    _toolOutputRafHandles.delete(tabId);
  }
}

/** Take (and clear) the buffered text for a tab. Returns "" when empty. */
export function takeBufferedStreamingChunk(tabId: TabId): string {
  const buffered = _streamBuffers.get(tabId) ?? "";
  _streamBuffers.delete(tabId);
  return buffered;
}

/** True when there is pending buffered streaming text for a tab. */
export function hasBufferedStreamingChunk(tabId: TabId): boolean {
  const buffered = _streamBuffers.get(tabId);
  return buffered !== undefined && buffered.length > 0;
}

/**
 * Schedule a flush of the streaming buffer, if one is not already scheduled.
 * `flush` is invoked with the tab id. Tiered (perf): the active (foreground)
 * tab on a visible page flushes on the next animation frame (~16ms, unchanged);
 * a background streaming tab downshifts to `BACKGROUND_FLUSH_MS`, and a hidden
 * page to `HIDDEN_FLUSH_MS`. `isActive` is whether `tabId` is the currently
 * active tab. Falls back to a 16ms timer for the active/visible case in
 * non-DOM/test environments where `requestAnimationFrame` is unavailable, so
 * behaviour is deterministic under unit tests.
 *
 * ── Backfill (transcript-so-far) suppression ──────────────────────────────
 * When `backfill === true` (the chunk is part of a broadcaster cursor=0
 * replay), behaves identically to `scheduleRoundChunkFlush` in backfill mode:
 * the underlying coalescing buffer keeps accumulating, but NO scheduled flush
 * is registered. Commit happens at one of: (a) `applyFrame`'s pre-non-chunk
 * sync flush (when a non-chunk frame breaks the run, preserving
 * chunk-before-tool_call ordering), or (b) the backfill→live boundary, or
 * (c) terminal close. Net paint count = "one per contiguous chunk run" — the
 * user sees blocks of text reveal instantly, not characters typing one by
 * one. See `scheduleRoundChunkFlush`'s docstring for the full rationale.
 */
export function scheduleStreamingFlush(
  tabId: TabId,
  flush: (tabId: TabId) => void,
  isActive = true,
  backfill = false,
): void {
  if (backfill) {
    return;
  }
  const run = (): void => {
    _streamRafHandles.delete(tabId);
    flush(tabId);
  };
  // Already scheduled? Upgrade-only when the current tiering would yield a
  // higher-fidelity handle (mirror of `scheduleRoundChunkFlush`; see
  // `reconcilePendingFlush`). Otherwise coalesce into the pending flush.
  if (reconcilePendingFlush(_streamRafHandles, tabId, run, isActive)) {
    return;
  }
  _streamRafHandles.set(tabId, scheduleTiered(run, isActive, tabId));
}

/** Cancel any pending streaming flush for a tab (e.g. on terminal flush). */
export function cancelStreamingFlush(tabId: TabId): void {
  const handle = _streamRafHandles.get(tabId);
  if (handle !== undefined) {
    cancelScheduled(handle);
    _streamRafHandles.delete(tabId);
  }
}

/** Drop all buffered streaming state for a tab (close/reset). Clears BOTH the
 *  bottom turn buffer and the per-round buffer so neither can leak into a later
 *  tab/turn. */
export function clearStreamingBuffer(tabId: TabId): void {
  cancelStreamingFlush(tabId);
  _streamBuffers.delete(tabId);
  cancelRoundChunkFlush(tabId);
  _roundChunkBuffers.delete(tabId);
  cancelToolOutputFlush(tabId);
  _toolOutputBuffers.delete(tabId);
}

/** Test-only reset hook for the transient handle maps. */
export function _resetChatTabsTransient(): void {
  for (const ctrl of _abortControllers.values()) {
    try {
      ctrl.abort();
    } catch {
      // ignore
    }
  }
  _abortControllers.clear();
  for (const ws of _webSockets.values()) {
    try {
      ws.close();
    } catch {
      // ignore
    }
  }
  _webSockets.clear();
  // Block 2 — abort + drop any sub-agent SSE subscription controllers so
  // tests do not leak live fetch streams across cases. The composite-keyed
  // structure is `Map<TabId, Map<subagentId, AbortController>>`; iterate every
  // inner map (every sub-agent under every tab) so all controllers are
  // aborted, regardless of how they were keyed (legacy sentinel vs ref).
  for (const inner of _subAgentStreamControllers.values()) {
    for (const ctrl of inner.values()) {
      try {
        ctrl.abort();
      } catch {
        // ignore
      }
    }
  }
  _subAgentStreamControllers.clear();
  for (const tabId of _streamRafHandles.keys()) {
    cancelStreamingFlush(tabId);
  }
  _streamRafHandles.clear();
  _streamBuffers.clear();
  for (const tabId of _roundChunkRafHandles.keys()) {
    cancelRoundChunkFlush(tabId);
  }
  _roundChunkRafHandles.clear();
  _roundChunkBuffers.clear();
  for (const tabId of _toolOutputRafHandles.keys()) {
    cancelToolOutputFlush(tabId);
  }
  _toolOutputRafHandles.clear();
  _toolOutputBuffers.clear();
  _liveSubAgentTabs.clear();
  // Block 2 — reset the sub-agent from_seq tracker (per-(tabId, subagentId)
  // highest applied broadcaster sequence) so specs start clean.
  _resetSubAgentLastAppliedSeq();
}

/** Get the AbortController for a tab (creates a fresh one if missing). */
export function getOrCreateAbortController(tabId: TabId): AbortController {
  const existing = _abortControllers.get(tabId);
  if (existing !== undefined) {
    return existing;
  }
  const fresh = new AbortController();
  _abortControllers.set(tabId, fresh);
  return fresh;
}

/** Replace the AbortController for a tab (after a turn finishes). */
export function resetAbortController(tabId: TabId): AbortController {
  const old = _abortControllers.get(tabId);
  if (old !== undefined) {
    try {
      old.abort();
    } catch {
      // ignore
    }
  }
  const fresh = new AbortController();
  _abortControllers.set(tabId, fresh);
  return fresh;
}

/** Read the AbortController without creating one. */
export function peekAbortController(tabId: TabId): AbortController | null {
  return _abortControllers.get(tabId) ?? null;
}

/** Drop an AbortController entry (called on tab close). */
export function clearAbortController(tabId: TabId): void {
  const old = _abortControllers.get(tabId);
  if (old !== undefined) {
    try {
      old.abort();
    } catch {
      // ignore
    }
    _abortControllers.delete(tabId);
  }
}

/** Attach a WebSocket handle for a tab. */
export function attachWebSocket(tabId: TabId, ws: WebSocket): void {
  const old = _webSockets.get(tabId);
  if (old !== undefined && old !== ws) {
    try {
      old.close();
    } catch {
      // ignore
    }
  }
  _webSockets.set(tabId, ws);
}

/** Read the current WebSocket handle for a tab. */
export function peekWebSocket(tabId: TabId): WebSocket | null {
  return _webSockets.get(tabId) ?? null;
}

/** Drop a WebSocket handle (and close it). */
export function clearWebSocket(tabId: TabId): void {
  const old = _webSockets.get(tabId);
  if (old !== undefined) {
    try {
      old.close();
    } catch {
      // ignore
    }
    _webSockets.delete(tabId);
  }
}

// ---------------------------------------------------------------------------
// Sub-agent live-stream subscription handles (block 2)
// ---------------------------------------------------------------------------

/** Register the AbortController driving a sub-agent's SSE subscription.
 *  Aborts + replaces any prior controller for the SAME `(tabId, subagentId)`
 *  pair so a re-open does not leak the previous connection.
 *
 *  `subagentId` is OPTIONAL — when omitted, the controller is stored under the
 *  LEGACY sentinel inner key, so single-arg bridge callers (`attachSubAgentStream(tabId, ctrl)`)
 *  see byte-equivalent behaviour to a single-layer Map (one entry per tab id,
 *  abort-on-replace, etc.). Ref-aware callers pass the explicit `subagentId`
 *  and get one isolated entry per sub-agent. Multiple sub-agents under the
 *  SAME tab id do NOT interfere with each other (their controllers live in
 *  separate inner-map entries).
 *
 *  @deprecated Single-argument form (no `subagentId`) is a legacy bridge
 *  only — once every call site threads the ref's `subagentId`, the argument
 *  will be made required.
 */
export function attachSubAgentStream(
  tabId: TabId,
  controller: AbortController,
  subagentId?: string,
): void {
  const inner = controllerInner(tabId);
  const innerKey = subagentId ?? LEGACY_SUBAGENT_KEY;
  const old = inner.get(innerKey);
  if (old !== undefined && old !== controller) {
    try {
      old.abort();
    } catch {
      // ignore
    }
  }
  inner.set(innerKey, controller);
}

/** Read the controller for a `(tabId, subagentId)` without creating one.
 *  Single-arg call (legacy bridge) returns the controller stored under the
 *  legacy sentinel, or — if absent — the first controller registered under
 *  the tab (which preserves single-arg parity: when a caller has attached
 *  only one controller per tab, "the controller for this tab" is
 *  unambiguous).
 *
 *  Ref-aware helper used by callers that thread `subagentId` through
 *  (concurrent sub-agents under the same tab); single-arg bridge remains
 *  for the closeTab teardown path. */
export function peekSubAgentStream(
  tabId: TabId,
  subagentId?: string,
): AbortController | undefined {
  const inner = _subAgentStreamControllers.get(tabId);
  if (inner === undefined) return undefined;
  if (subagentId !== undefined) {
    return inner.get(subagentId);
  }
  const legacy = inner.get(LEGACY_SUBAGENT_KEY);
  if (legacy !== undefined) return legacy;
  // Fallback: legacy single-arg readers want "the" controller for this tab.
  // Return the first entry's value (insertion order) so the answer is stable.
  const first = inner.values().next();
  return first.done ? undefined : first.value;
}

/** Abort + drop a sub-agent's SSE subscription (called on tab close).
 *  Also clears the matching live-tier marker so a closed tab cannot keep a
 *  reused tab id raised on the `SUBAGENT_LIVE_FLUSH_MS` tier. Idempotent.
 *
 *  Composite-key behaviour:
 *    - With `subagentId` → tears down ONLY that specific sub-agent (its
 *      controller + its live marker), leaving any sibling sub-agents under
 *      the same tab untouched.
 *    - Without `subagentId` (legacy bridge) → tears down ALL sub-agent
 *      controllers + live markers owned by the tab, in one shot. This is
 *      REQUIRED because `closeTab(tabId)` calls `clearSubAgentStream(tabId)`
 *      (no subagentId), and that one call must reclaim every nested
 *      sub-agent the tab owned — byte-equivalent to a single-layer-map
 *      teardown.
 *
 *  NOTE: a resume RE-SUBSCRIBE does NOT go through here — it calls
 *  `attachSubAgentStream` directly (which aborts the prior controller without
 *  touching the marker), and the new subscription re-marks at its entry — so
 *  unmarking here is safe (close-only) and never races a live re-subscribe.
 *
 *  @deprecated Single-argument form (no `subagentId`) is a legacy bridge
 *  only — once every call site threads the ref's `subagentId`, the argument
 *  will be made required and the "clear all under this tab" behaviour will
 *  move to a dedicated `clearAllSubAgentStreams(tabId)` helper.
 */
export function clearSubAgentStream(
  tabId: TabId,
  subagentId?: string,
): void {
  const inner = _subAgentStreamControllers.get(tabId);
  if (inner === undefined) {
    // Defensive: still drop any orphan live markers under this tab so a
    // close-after-marker race is handled — the unmark should run
    // unconditionally before checking the controller map.
    unmarkSubAgentTabLive(tabId, subagentId);
    return;
  }
  if (subagentId === undefined) {
    // Legacy single-arg: abort + drop EVERY controller this tab owns and
    // clear ALL live markers — byte-equivalent to a single-layer-map entry
    // path (`unmarkSubAgentTabLive(tabId)` + `controllers.delete(tabId)`).
    unmarkSubAgentTabLive(tabId);
    for (const ctrl of inner.values()) {
      try {
        ctrl.abort();
      } catch {
        // ignore
      }
    }
    _subAgentStreamControllers.delete(tabId);
    return;
  }
  // Ref-aware: tear down ONLY this sub-agent.
  unmarkSubAgentTabLive(tabId, subagentId);
  const ctrl = inner.get(subagentId);
  if (ctrl !== undefined) {
    try {
      ctrl.abort();
    } catch {
      // ignore
    }
    inner.delete(subagentId);
    if (inner.size === 0) {
      _subAgentStreamControllers.delete(tabId);
    }
  }
}
