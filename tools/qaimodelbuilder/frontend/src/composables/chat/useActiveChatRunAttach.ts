// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { wsBaseUrl } from "@/api";
import { useChatTabsStore, type TabId } from "@/stores/chatTabs";
import type { ChatStreamFrame } from "@/types/streaming";
import { watch } from "vue";

const sockets = new Map<TabId, WebSocket>();
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

export function attachActiveChatRun(tabId: TabId, attachPath: string): void {
  ensurePruneWatcher();
  const existing = sockets.get(tabId);
  if (existing !== undefined && existing.readyState <= WebSocket.OPEN) {
    return;
  }
  const store = useChatTabsStore();
  const ws = new WebSocket(activeRunWsUrl(attachPath));
  sockets.set(tabId, ws);
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
      store.confirmDone(tabId);
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
      store.recordError(tabId, {
        type: String(err.type ?? "ActiveRunAttachError"),
        code: String(err.code ?? "chat.active_run_attach_error"),
        message: String(err.message ?? "Active run stream failed"),
      });
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
  };
  ws.onerror = () => {
    store.recordError(tabId, {
      type: "ActiveRunAttachError",
      code: "chat.active_run_attach_error",
      message: "Active run stream failed",
    });
  };
}

export function stopActiveChatRunAttach(tabId: TabId): void {
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
