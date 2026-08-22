// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { wsBaseUrl, apiJson } from "@/api";
import { useChatTabsStore, type TabId } from "@/stores/chatTabs";
import type { ChatStreamFrame } from "@/types/streaming";
import type { ActiveRunItemWire } from "@/composables/chat/useActiveChatRuns";
import { watch } from "vue";
// N.46 — cross-channel WS coordination (page visibility / online-offline).
import {
  registerWebSocketChannel,
  type WebSocketChannelRegistration,
} from "@/composables/chat/useWebSocketOrchestrator";

const sockets = new Map<TabId, WebSocket>();
/** Orchestrator registration per attached tab (N.46). Lives beside `sockets`
 *  because it shares that socket's lifetime exactly. */
const orchestratorRegs = new Map<TabId, WebSocketChannelRegistration>();
let pruneWatcherInstalled = false;

function activeRunWsUrl(attachPath: string): string {
  const path = attachPath.startsWith("/") ? attachPath : `/${attachPath}`;
  const base = wsBaseUrl();
  return base === "" ? path : `${base}${path}`;
}

interface ActiveRunWsEnvelope {
  type?: string;
  /** Set by the broadcaster's cursor replay when this frame is part of the
   *  buffered transcript-so-far (NOT a live delta the user can watch appear).
   *  Wire shape comes from `interfaces/http/routes/chat/_ws.py:active_run_ws`
   *  → `ChatStreamBroadcaster.replay` which marks each replayed
   *  `ChatStreamReplayFrame` with `backfill=not live` (chat_stream_broadcaster.py:216).
   *  The frontend MUST forward this to `applyFrame` so the coalescing layer
   *  suppresses per-frame flushes — otherwise the trailing transcript the user
   *  has already seen rendered via the HTTP snapshot (`loadHistoryMessages`,
   *  see `useActiveChatRuns.openRun`) would be replayed逐段, identical to the
   *  sub-agent "已经看过的最后一段历史又被打字机重播" bug (which was the same
   *  reducer-chain bug in a different subscriber). */
  backfill?: boolean;
  frame?: ChatStreamFrame;
  error?: { type?: string; code?: string; message?: string };
}

/** Optional lifecycle hooks for a re-attach. Used by the data-plane transport
 *  resume path (`useChatTransport.sendWs` → `onResumeNeeded`) to settle its
 *  in-flight turn promise on the attach's terminal, and to fall back
 *  gracefully when the active run does not exist (turn ended before the drop,
 *  or broadcaster TTL expired) — in which case the WS closes WITHOUT a
 *  terminal `done`/`error` and the tab would otherwise stay stuck streaming.
 *  The manual active-runs popover path calls the 2-arg form and needs none. */
export interface AttachActiveChatRunCallbacks {
  /** Fired on the broadcaster's terminal `done` (after backfill commit). */
  onDone?(): void;
  /** Fired on the broadcaster's terminal `error` (after backfill commit). */
  onError?(err: { type: string; code: string; message: string }): void;
  /** Fired when the socket closes WITHOUT ever delivering a terminal
   *  `done`/`error` (no active run to attach to / connection lost before
   *  terminal). The caller degrades gracefully (e.g. SSE replay or a
   *  retryable error) rather than leaving the tab pinned to `streaming`. */
  onClosedWithoutTerminal?(): void;
}

export function attachActiveChatRun(
  tabId: TabId,
  attachPath: string,
  callbacks?: AttachActiveChatRunCallbacks,
): void {
  ensurePruneWatcher();
  const existing = sockets.get(tabId);
  if (existing !== undefined && existing.readyState <= WebSocket.OPEN) {
    return;
  }
  const store = useChatTabsStore();
  const ws = new WebSocket(activeRunWsUrl(attachPath));
  sockets.set(tabId, ws);
  // N.46 — join the page-wide WS orchestrator.
  //
  // This channel has no retry loop of its own by design (a premature close
  // degrades via `onClosedWithoutTerminal`), so what it needs from the
  // orchestrator is not backoff but the two page-level edges:
  //
  //   onSuspend — the page is offline / hidden ≥60s. Drop the socket so the
  //     server stops holding a replay generator (and the proxy an idle
  //     connection) for a tab that cannot paint. `suspended` suppresses the
  //     `onClosedWithoutTerminal` fallback: this close is OUR doing, not a
  //     lost run, so the tab must NOT be pushed out of `streaming` with a
  //     "请重新发送" error. The backend keeps the turn running and buffers
  //     every frame in the broadcaster, so nothing is lost.
  //   onResume — re-attach exactly the way the transport's own resume path
  //     does (`resetTurnForResume` then attach) so the `from_seq=0` backfill
  //     is the single source of truth for the rendered turn — no duplicated
  //     bubbles. Skipped when the turn settled while we were away.
  //
  // De-duped by tab id: the early-return above means at most one live socket
  // (hence one registration) per tab.
  let suspended = false;
  const priorReg = orchestratorRegs.get(tabId);
  if (priorReg !== undefined) {
    priorReg.unregister();
  }
  orchestratorRegs.set(
    tabId,
    registerWebSocketChannel(`active-run-ws:${tabId}`, {
      onSuspend: () => {
        suspended = true;
        try {
          ws.close(1000, "client suspended by orchestrator");
        } catch {
          // already closing/closed
        }
      },
      onResume: () => {
        if (!suspended) {
          return;
        }
        suspended = false;
        const tab = store.tabs.find((t) => t.id === tabId);
        // The turn settled (or the tab went away) during the outage — nothing
        // to resume. `applyFrame` gates on `streaming`, so re-attaching a
        // settled tab would replay a transcript into a closed turn.
        if (tab === undefined || tab.status !== "streaming") {
          return;
        }
        store.resetTurnForResume(tabId);
        attachActiveChatRun(tabId, attachPath, callbacks);
      },
    }),
  );
  // Set once a terminal `done`/`error` envelope has been observed, so the
  // `onclose` handler can tell an ordinary post-terminal close (nothing more
  // to do) apart from a premature close (no active run / lost connection →
  // `onClosedWithoutTerminal` so the caller can fall back).
  let sawTerminal = false;
  // Tracks whether the most recent frame applied through this attach was a
  // backfill (cursor=0 replay) frame. The backfill→live boundary, the
  // terminal `done`/`error` envelope, and an unexpected `close` each commit
  // the accumulated batch. See the matching `flushBackfill` path in
  // `chatTabs._subscribeSubAgentStream` — same bug class, same fix.
  //
  // ── Honest paint-count semantics ─────────────────────────────────────
  // `applyFrame` synchronously flushes coalescing buffers before every
  // NON-chunk frame to keep handler-observed state up to date (ordering
  // invariant for `tool_call.lead_in` etc., not relaxed for backfill). So a
  // backfill burst that interleaves chunks with tool_call frames produces
  // ONE paint per contiguous chunk run, not a single paint for the whole
  // burst. This still eliminates the user-reported "typewriter replay"
  // — going from per-frame (character-level) to per-block (instant reveal)
  // paints crosses the perceptual batching threshold; see the sub-agent
  // counterpart for the full rationale.
  let inBackfill = false;
  const flushBackfill = (): void => {
    if (!inBackfill) return;
    inBackfill = false;
    store.flushRoundChunkNow(tabId);
    store.flushStreamingNow(tabId);
  };
  ws.onmessage = (event: MessageEvent<string>) => {
    let envelope: ActiveRunWsEnvelope;
    try {
      envelope = JSON.parse(event.data) as ActiveRunWsEnvelope;
    } catch {
      return;
    }
    if (envelope.type === "frame" && envelope.frame !== undefined) {
      const isBackfill = envelope.backfill === true;
      // Boundary commit: the first live frame after a backfill burst settles
      // the accumulated history BEFORE the live append runs through the
      // reducer, so the live content sits on top of committed history rather
      // than concurrent pending buffer (which would otherwise corrupt
      // ordering on the round_index code path).
      if (inBackfill && !isBackfill) {
        flushBackfill();
      }
      inBackfill = isBackfill;
      store.applyFrame(tabId, envelope.frame, isBackfill);
      return;
    }
    if (envelope.type === "done") {
      // Terminal: ensure any backfill buffer is committed (cold-attach to a
      // run that finished while we were still backfilling — broadcaster
      // replays the full transcript then sends `done` without any live frame
      // ever arriving). Same idempotency as the sub-agent path.
      flushBackfill();
      sawTerminal = true;
      store.confirmDone(tabId);
      callbacks?.onDone?.();
      ws.close();
      return;
    }
    if (envelope.type === "error") {
      const err = envelope.error ?? {
        type: "ActiveRunAttachError",
        code: "chat.active_run_attach_error",
        message: "Active run stream failed",
      };
      flushBackfill();
      sawTerminal = true;
      const normalized = {
        type: String(err.type ?? "ActiveRunAttachError"),
        code: String(err.code ?? "chat.active_run_attach_error"),
        message: String(err.message ?? "Active run stream failed"),
      };
      store.recordError(tabId, normalized);
      callbacks?.onError?.(normalized);
      ws.close();
    }
  };
  ws.onclose = () => {
    // Unexpected close path also commits any pending backfill so the tab
    // does not show a partial transcript (no-op when not in backfill mode).
    flushBackfill();
    if (sockets.get(tabId) === ws) {
      sockets.delete(tabId);
    }
    // N.46 — an orchestrator-initiated suspend close is NOT a lost run: the
    // turn keeps running server-side and `onResume` re-attaches. Firing the
    // fallback here would push the tab out of `streaming` with a "请重新发送"
    // error every time the user backgrounded the page for a minute.
    if (suspended) {
      return;
    }
    // A terminal was seen, or the run is genuinely gone — either way this
    // attach is over, so stop taking part in the page-level coupling.
    orchestratorRegs.get(tabId)?.unregister();
    orchestratorRegs.delete(tabId);
    // Premature close: the socket closed WITHOUT a terminal `done`/`error`.
    // Either there was no active run to attach to (the turn ended before the
    // drop, or the broadcaster TTL expired) or the re-attach connection
    // itself dropped. Let the caller degrade gracefully instead of leaving
    // the tab pinned to `streaming`. When a terminal was already seen this is
    // an ordinary post-terminal close — do not fire.
    if (!sawTerminal) {
      callbacks?.onClosedWithoutTerminal?.();
    }
  };
  ws.onerror = () => {
    // Do NOT record an error here: an `error` event is almost always followed
    // by a `close`, and a premature close (no active run) must degrade via the
    // caller's `onClosedWithoutTerminal` fallback rather than pin a hard error
    // onto the tab. A genuine terminal `error` envelope arrives on `onmessage`
    // and is handled there. `onclose` owns the fallback decision.
  };
}

interface ActiveRunsResponseWire {
  items?: ActiveRunItemWire[];
}

/**
 * Re-attach an already-open MAIN-agent conversation tab to its still-running
 * background turn, if one exists — the re-open counterpart of the mid-turn
 * resume in `useChatTransport`.
 *
 * Root-cause fix for "关闭标签页再打开(或侧边栏重开会话)后,顺序恢复正常但
 * 之前那个 question 工具调用丢失了": the sidebar / `/open` re-open paths only
 * call `store.loadHistoryMessages` (a STATIC snapshot of what was persisted).
 * Since the backend no longer kills a turn whose data-plane WS dropped, that
 * turn keeps running in the background — and a `question` it raised is still
 * pending (answerable via the control plane) but INVISIBLE, because the static
 * history load never re-attaches to the live broadcaster. This helper closes
 * that gap: after history is loaded, it checks `GET /api/chat/active-runs` for
 * a live `chat` run bound to this conversation (or tab) and, if found,
 * `setStreaming` + `attachActiveChatRun` — replaying the transcript-so-far
 * (including the pending `question` card) and following live frames.
 *
 * Mirrors `useActiveChatRuns.openRun`'s "loadHistory → (attach_path)
 * setStreaming + attach" sequence so there is ONE re-attach mechanism.
 *
 * Idempotent + non-clobbering:
 *   - `attachActiveChatRun` de-dups an already-OPEN socket for the tab.
 *   - When NO matching openable run exists, does NOTHING — the tab keeps its
 *     static history and stays `idle` (original behaviour unchanged).
 *   - Best-effort: a missing conversation id, a transient fetch failure, or
 *     the tab going away are all swallowed so a re-open never fails on this.
 *
 * Call AFTER `loadHistoryMessages` so the backfill sits on top of the painted
 * snapshot (same ordering guarantee as `openRun`).
 */
export async function resumeConversationIfRunning(
  tabId: TabId,
  conversationId: string | null,
): Promise<void> {
  if (conversationId === null || conversationId === "") {
    return;
  }
  const store = useChatTabsStore();
  const tab = store.tabs.find((t) => t.id === tabId);
  // Only main-agent tabs resume here; a sub-agent tab has its own live stream
  // path (`_subscribeSubAgentStream`).
  if (tab === undefined || tab.kind === "subagent") {
    return;
  }
  let items: ActiveRunItemWire[];
  try {
    const res = await apiJson<ActiveRunsResponseWire>(
      "GET",
      "/api/chat/active-runs",
    );
    items = Array.isArray(res.items) ? res.items : [];
  } catch {
    // Transient failure — leave the tab on its static history (unchanged).
    return;
  }
  // Match a live CHAT run for THIS conversation (or, defensively, this tab)
  // that the backend says is re-attachable.
  const run = items.find(
    (it) =>
      it.kind === "chat" &&
      it.openable &&
      it.attach_path !== null &&
      (it.conversation_id === conversationId || it.tab_id === tabId),
  );
  if (run === undefined || run.attach_path === null) {
    return;
  }
  // Promote to streaming so `applyFrame` (which gates on `status ===
  // "streaming"`) accepts the backfill, then re-attach. The tab already holds
  // the static snapshot; the broadcaster replays the transcript-so-far as
  // backfill (batched, no typewriter) and follows live frames — restoring the
  // pending question card and letting the turn finish.
  store.setStreaming(tabId);
  attachActiveChatRun(tabId, run.attach_path);
}

export function stopActiveChatRunAttach(tabId: TabId): void {
  // N.46 — release the orchestrator registration BEFORE closing, so the
  // resulting `onclose` cannot re-arm anything for a deliberately stopped
  // attach (a closed tab must never be re-attached by a page-level resume).
  orchestratorRegs.get(tabId)?.unregister();
  orchestratorRegs.delete(tabId);
  const ws = sockets.get(tabId);
  if (ws !== undefined) {
    ws.close();
    sockets.delete(tabId);
  }
}

function ensurePruneWatcher(): void {
  if (pruneWatcherInstalled) return;
  pruneWatcherInstalled = true;
  const store = useChatTabsStore();
  watch(
    () => store.tabs.map((t) => t.id),
    (ids) => {
      const live = new Set(ids);
      for (const tabId of [...sockets.keys()]) {
        if (!live.has(tabId)) {
          stopActiveChatRunAttach(tabId);
        }
      }
    },
  );
}
