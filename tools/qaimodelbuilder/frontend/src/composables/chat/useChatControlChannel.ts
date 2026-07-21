// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useChatControlChannel — page-global chat control-plane WebSocket client.
 *
 * Why this exists (root-cause of the "438-second answer hang")
 * ------------------------------------------------------------
 * The chat surface has two distinct planes of browser↔server traffic:
 *
 * * **Data plane** — long-lived server→client token / tool_call / tool_result
 *   / done streams that drive the streaming view. By default these ride a
 *   per-tab SSE connection (`useChatTransport` → `apiSSE`, one open connection
 *   for the lifetime of each in-flight turn).
 * * **Control plane** — short client→server signals scoped to an in-flight
 *   turn: `answer` (resolve a suspended blocking `question` tool) and `stop`
 *   (cooperative abort). Historically these went out as plain
 *   `POST /api/chat/answer` / `POST /api/chat/stop` REST calls.
 *
 * The browser caps same-origin **HTTP/1.1** connections at 6 (hard-coded in
 * every mainstream engine). When several tabs each hold an SSE stream open the
 * pool is saturated by data-plane streams, so a subsequent control-plane POST
 * is **queued client-side** until one of those streams releases its slot —
 * producing the reported minute-scale "my answer never arrives" hangs (a
 * 438-second wait between dialog-close and the suspended `question` resuming).
 * The backend `answer` handler itself is instantaneous (a synchronous
 * `question_registry.resolve()` that just pops a future and calls
 * `set_result`), so the entire delay is the client-side connection-pool queue,
 * NOT any server await — this client removes that queue.
 *
 * A WebSocket has its own socket and is NOT subject to the HTTP/1.1 6-socket
 * pool, so control frames sent over it can never be queued behind SSE traffic.
 * The backend already exposes the matching endpoint (`WS /api/chat/control`,
 * see `interfaces/http/routes/chat/_control_ws.py`); this module is the
 * front-end client for it.
 *
 * Design (kept minimal — AGENTS.md 判据 1 / §🔴 State-Truth-First)
 * --------------------------------------------------------------
 * 1. **Page-global singleton** — ONE WebSocket per browser page, multiplexing
 *    every chat tab's control frames (each frame carries its own `tab_id`).
 *    The global WS connection budget therefore stays at exactly 1 regardless
 *    of how many tabs are open — no per-tab socket growth, no leak.
 * 2. **REST is always the fallback** — `answer` / `stop` still go out as the
 *    existing `POST /api/chat/answer` / `POST /api/chat/stop` when the control
 *    WS is not `ready` (initial page-load race, reconnect window, test /
 *    TestClient flows). The locked REST routes (AGENTS.md §3.1) are untouched
 *    and behaviour never changes — the WS is purely an unqueued fast path.
 * 3. **Self-healing lifecycle** — lazy connect on first use, auto-reconnect
 *    with capped exponential backoff (mirrors `useChatWebSocket`), and the
 *    handshake (`hello`) gates "ready". Because REST always backs it up, a
 *    half-open / reconnecting WS never wedges a control action (the caller
 *    falls back to REST), satisfying State-Truth-First (the WS readiness flag
 *    reflects a real probe — the `hello` handshake — not an optimistic guess).
 *
 * Wire protocol (locked in `_control_ws.py` module docstring)
 * -----------------------------------------------------------
 *   server → client (handshake): `{type:"hello", protocol:"qai.chat.control/1"}`
 *   client → server : `{type:"answer", tab_id, answer, id?}`
 *                     `{type:"stop",   tab_id, reason?, id?}`
 *   server → client : `{type:"ack",   id, kind, tab_id, ok, result}`
 *                     `{type:"error", id, error}`
 *
 * Acks are fire-and-forget from the caller's perspective (the control action
 * already executed server-side); we observe them only for debug logging.
 */
import { wsBaseUrl } from "@/api";

const CONTROL_PATH = "/api/chat/control";
const PROTOCOL = "qai.chat.control/1";
const MAX_RECONNECT = 5;
const BACKOFF_BASE_MS = 200;

export type ControlChannelState =
  | "idle"
  | "connecting"
  | "ready"
  | "closed";

export interface ChatControlChannel {
  /** Current connection state (real, probe-backed — `ready` only after `hello`). */
  readonly state: () => ControlChannelState;
  /** Open the connection if not already open/opening. Idempotent. */
  ensureOpen(): void;
  /**
   * Open the channel (if needed) and resolve once it reaches `ready` (the
   * `hello` handshake completed), or `false` if it does not become ready
   * within `timeoutMs`. Used by callers that MUST send a frame the moment a
   * user acts (e.g. mid-turn `inject`) and cannot rely on a REST fallback:
   * lazily-opened WS is still `connecting` on the same tick `ensureOpen()`
   * returns, so a synchronous `sendInject` would silently fail. Resolves
   * `true` immediately when already `ready`.
   */
  whenReady(timeoutMs: number): Promise<boolean>;
  /**
   * Send an `answer` frame for `tabId`. Returns `true` iff it was actually
   * dispatched over a `ready` WebSocket; `false` means the caller MUST fall
   * back to the REST endpoint (the WS is not ready).
   */
  sendAnswer(tabId: string, answer: string): boolean;
  /**
   * Send a `stop` frame for `tabId`. Returns `true` iff it was actually
   * dispatched over a `ready` WebSocket; `false` ⇒ fall back to REST.
   */
  sendStop(tabId: string, reason: string): boolean;
  /**
   * Send an `inject` frame for `tabId` (mid-turn user injection, V2
   * enhancement). Returns `true` iff it was dispatched over a `ready`
   * WebSocket. Unlike `answer`/`stop` there is NO REST fallback — an
   * injection only makes sense for a live in-process stream — so a `false`
   * return means the caller must rely on its local message-queue fallback
   * (the un-injected text is re-sent as a fresh turn when the turn ends).
   *
   * Image parity: images ride INSIDE `text` as `![](url)` markdown (the same
   * shape a normal submit uses), so this frame needs no separate media field —
   * the backend extracts the refs from the text and resolves them to vision
   * blocks at the inter-round seam.
   */
  sendInject(tabId: string, text: string): boolean;
  /**
   * Send an `inject_cancel` frame for `tabId` (V2 enhancement): withdraw a
   * not-yet-folded injection whose pending bubble the user just edited or
   * cancelled, so the live run does not also fold the same text in. Returns
   * `true` iff dispatched over a `ready` WebSocket; `false` is a benign
   * no-op (the injection may already be drained) -- no REST fallback.
   */
  sendInjectCancel(tabId: string, text: string): boolean;
  /**
   * Send a `retry_now` frame for `tabId` (V2 enhancement): cut short the
   * in-flight network-retry backoff so the turn re-opens the LLM stream
   * immediately (the "立即重试" button after the user manually restored
   * connectivity). Returns `true` iff dispatched over a `ready` WebSocket;
   * `false` is a benign no-op (nothing waiting) — NO REST fallback (it only
   * makes sense for a live mid-backoff stream).
   */
  sendRetryNow(tabId: string): boolean;
  /**
   * Send a `cancel_tool` frame for `tabId` + `callId` (per-call single-tool
   * cancel). Returns `true` iff dispatched over a `ready` WebSocket; `false`
   * ⇒ fall back to `POST /api/chat/cancel_tool`. Unlike `stop` this does not
   * abort the turn — the backend cancels just that one tool and continues.
   */
  sendCancelTool(tabId: string, callId: string): boolean;
  /** Close the connection cleanly (page unload / test teardown). */
  close(): void;
}

interface ControlChannelOptions {
  /** Test injection — defaults to `globalThis.WebSocket`. */
  readonly wsCtor?: typeof WebSocket;
  /** Test injection — backoff base in ms (default 200). */
  readonly backoffBaseMs?: number;
  /** Test injection — max reconnect attempts (default 5). */
  readonly maxReconnect?: number;
}

function buildControlUrl(): string {
  const base = wsBaseUrl();
  if (base === "") {
    return CONTROL_PATH;
  }
  return `${base}${CONTROL_PATH}`;
}

function isHello(raw: unknown): boolean {
  if (raw === null || typeof raw !== "object") {
    return false;
  }
  const o = raw as Record<string, unknown>;
  return o["type"] === "hello" && o["protocol"] === PROTOCOL;
}

/**
 * Create a control-channel client. Exported for tests; production code uses
 * the page-global singleton via {@link useChatControlChannel}.
 */
export function createChatControlChannel(
  options: ControlChannelOptions = {},
): ChatControlChannel {
  const ctor: typeof WebSocket =
    options.wsCtor ?? (globalThis.WebSocket as typeof WebSocket);
  const backoffBaseMs = options.backoffBaseMs ?? BACKOFF_BASE_MS;
  const maxReconnect = options.maxReconnect ?? MAX_RECONNECT;

  let state: ControlChannelState = "idle";
  let socket: WebSocket | null = null;
  let reconnectAttempt = 0;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let manualClose = false;
  // Monotonic id stamped on each frame so a future debug/UX hook can correlate
  // an `ack`/`error` with the frame that caused it. Not load-bearing today.
  let msgSeq = 0;

  function clearReconnectTimer(): void {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  }

  function scheduleReconnect(): void {
    if (manualClose) {
      return;
    }
    if (reconnectAttempt >= maxReconnect) {
      // Give up reconnecting; REST fallback continues to serve control frames.
      // A later `ensureOpen()` (e.g. on the next user action) resets the
      // attempt counter and tries again.
      state = "closed";
      return;
    }
    const attempt = reconnectAttempt;
    reconnectAttempt = reconnectAttempt + 1;
    const delay = backoffBaseMs * Math.pow(2, attempt);
    clearReconnectTimer();
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      doConnect();
    }, delay);
  }

  function doConnect(): void {
    if (typeof ctor !== "function") {
      state = "closed";
      return;
    }
    state = "connecting";
    let ws: WebSocket;
    try {
      ws = new ctor(buildControlUrl());
    } catch {
      scheduleReconnect();
      return;
    }
    socket = ws;
    ws.addEventListener("message", (ev: MessageEvent) => {
      const data = ev.data;
      if (typeof data !== "string") {
        return;
      }
      let parsed: unknown;
      try {
        parsed = JSON.parse(data);
      } catch {
        return;
      }
      if (isHello(parsed)) {
        // Handshake complete — the channel is genuinely usable now.
        state = "ready";
        reconnectAttempt = 0;
        return;
      }
      // `ack` / `error` envelopes are fire-and-forget; nothing to do today.
    });
    ws.addEventListener("close", () => {
      socket = null;
      const wasUsable = state === "ready" || state === "connecting";
      state = "closed";
      if (manualClose) {
        return;
      }
      if (wasUsable) {
        scheduleReconnect();
      }
    });
    ws.addEventListener("error", () => {
      // The close handler runs afterwards and drives reconnect; ignore here.
    });
  }

  function ensureOpen(): void {
    if (state === "connecting" || state === "ready") {
      return;
    }
    manualClose = false;
    reconnectAttempt = 0;
    clearReconnectTimer();
    doConnect();
  }

  function whenReady(timeoutMs: number): Promise<boolean> {
    ensureOpen();
    if (state === "ready") {
      return Promise.resolve(true);
    }
    return new Promise<boolean>((resolve) => {
      const start = Date.now();
      const poll = (): void => {
        if (state === "ready") {
          resolve(true);
          return;
        }
        // `closed` after exhausting reconnects, or the timeout elapsed →
        // give up so the caller can surface a failure (no silent hang).
        if (state === "closed" || Date.now() - start >= timeoutMs) {
          resolve(false);
          return;
        }
        setTimeout(poll, 20);
      };
      poll();
    });
  }

  function trySend(frame: Record<string, unknown>): boolean {
    if (state !== "ready" || socket === null || socket.readyState !== 1) {
      return false;
    }
    try {
      socket.send(JSON.stringify(frame));
      return true;
    } catch {
      return false;
    }
  }

  function nextId(): string {
    msgSeq = msgSeq + 1;
    return `c-${msgSeq}`;
  }

  function sendAnswer(tabId: string, answer: string): boolean {
    return trySend({
      type: "answer",
      id: nextId(),
      tab_id: tabId,
      answer,
    });
  }

  function sendStop(tabId: string, reason: string): boolean {
    return trySend({
      type: "stop",
      id: nextId(),
      tab_id: tabId,
      reason,
    });
  }

  function sendInject(tabId: string, text: string): boolean {
    return trySend({
      type: "inject",
      id: nextId(),
      tab_id: tabId,
      text,
    });
  }

  function sendInjectCancel(tabId: string, text: string): boolean {
    return trySend({
      type: "inject_cancel",
      id: nextId(),
      tab_id: tabId,
      text,
    });
  }

  function sendRetryNow(tabId: string): boolean {
    return trySend({
      type: "retry_now",
      id: nextId(),
      tab_id: tabId,
    });
  }

  function sendCancelTool(tabId: string, callId: string): boolean {
    return trySend({
      type: "cancel_tool",
      id: nextId(),
      tab_id: tabId,
      call_id: callId,
    });
  }

  function close(): void {
    manualClose = true;
    clearReconnectTimer();
    if (socket !== null) {
      try {
        socket.close();
      } catch {
        // ignore
      }
      socket = null;
    }
    state = "closed";
  }

  return {
    state: () => state,
    ensureOpen,
    whenReady,
    sendAnswer,
    sendStop,
    sendInject,
    sendInjectCancel,
    sendRetryNow,
    sendCancelTool,
    close,
  };
}

// ---------------------------------------------------------------------------
// Page-global singleton
// ---------------------------------------------------------------------------

let singleton: ChatControlChannel | null = null;

/**
 * Access the page-global control channel, creating + opening it on first use.
 * One WebSocket for the whole page; callers route per-tab control frames over
 * it by passing the `tab_id`. Always pair a `true`/`false` return with a REST
 * fallback so a not-yet-ready channel never drops a control action.
 */
export function useChatControlChannel(): ChatControlChannel {
  if (singleton === null) {
    singleton = createChatControlChannel();
  }
  singleton.ensureOpen();
  return singleton;
}

/** Test-only reset hook — closes & clears the page-global channel. */
export function _resetChatControlChannel(): void {
  if (singleton !== null) {
    singleton.close();
    singleton = null;
  }
}
