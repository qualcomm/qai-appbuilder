// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useChatWebSocket — chat WebSocket client (PR-054).
 *
 * Implements the chat WS contract from api-contract.md §4.
 *
 *   Path     : `/api/chat/ws` (Vite dev proxy: `ws: true` per PR-050)
 *   Handshake: server → client `{type: "ready", session_id}` first
 *   Send     : client → server `{type: "send", prompt, conversation_id?}`
 *              client → server `{type: "stop"}`
 *   Receive  : server → client `{type: "frame", frame: {...}}`
 *              server → client `{type: "error", error: <envelope>}`
 *              server → client `{type: "done"}`
 *
 * Client may NOT send before the `ready` handshake. We buffer one
 * pending `send` while the handshake is in flight and flush after
 * `ready` arrives. After `error` / `done` the server closes the
 * connection; the client may reopen for a new turn.
 *
 * Reconnection: on unexpected close (no `error`/`done` envelope),
 * retry with 2^n backoff up to 5 times. After 5 failures the caller
 * is notified via `onGiveUp` so it can fall back to SSE
 * (see `useChatTransport`).
 *
 * The composable is connection-per-tab: each tab gets its own
 * `WebSocket` instance. Multiplexing onto a single connection is a
 * future optimisation; for now we trade a few sockets for absolute
 * isolation per refactor-plan §10.6 invariant 2.
 */
import { wsBaseUrl } from "@/api";
import type {
  ChatWsClientMessage,
  ChatWsServerMessage,
  ChatWsReady,
  ChatWsFrame,
  ChatWsError,
  ChatWsDone,
  ChatWsPing,
  ChatStreamFrame,
  ApiErrorPayload,
} from "@/types/streaming";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export type ChatWsState =
  | "idle"
  | "connecting"
  | "ready"
  | "closing"
  | "closed";

export interface ChatWsHandlers {
  onReady?(sessionId: string): void;
  onFrame?(frame: ChatStreamFrame): void;
  onError?(error: ApiErrorPayload): void;
  onDone?(): void;
  onClose?(ev: { code: number; reason: string; wasClean: boolean }): void;
  /**
   * Called after the client has exhausted its retry budget. Caller
   * (transport) is expected to switch to SSE when this fires.
   */
  onGiveUp?(): void;
}

/**
 * Mandatory upgrade-time query params for `/api/chat/ws`.
 *
 * The backend (`interfaces/http/routes/chat/_ws.py:92-93`) declares
 * `conversation_id` + `tab_id` as REQUIRED query params on the WS
 * route — a bare `/api/chat/ws` upgrade is rejected with HTTP 403
 * before the handshake completes. The client therefore MUST append
 * both to the connection URL (not just inside the `send` body, which
 * the route ignores for routing — it uses the handshake query). They
 * are URL-encoded by `buildWsUrl`.
 */
export interface ChatWsConnectParams {
  readonly conversationId: string;
  readonly tabId: string;
}

export interface UseChatWebSocketOptions {
  /** Path on the server. Defaults to `/api/chat/ws`. */
  readonly path?: string;
  /** Maximum reconnection attempts before `onGiveUp`. Default 5. */
  readonly maxReconnect?: number;
  /** Backoff base in ms. Default 200. */
  readonly backoffBaseMs?: number;
  /**
   * Optional alternate WebSocket constructor (test injection).
   * Defaults to `globalThis.WebSocket`.
   */
  readonly wsCtor?: typeof WebSocket;
  /**
   * Supplies the mandatory `conversation_id` + `tab_id` upgrade query
   * params for EVERY connect attempt (initial + every reconnect). The
   * caller (`useChatTransport`) provides the current tab's already-ensured
   * conversation id + tab id here. Returning `null` means the params are
   * not yet known; the connect is then skipped and the client gives up so
   * the caller can fall back (it must never open a query-less URL that the
   * backend would 403). Read fresh on each `doConnect` so a reconnect
   * always carries the same identifiers.
   */
  readonly getConnectParams?: () => ChatWsConnectParams | null;
}

export interface ChatWebSocketClient {
  readonly state: () => ChatWsState;
  readonly sessionId: () => string | null;
  readonly reconnectCount: () => number;
  /** Open the connection. Idempotent if already connecting/open. */
  open(handlers: ChatWsHandlers): void;
  /** Send a `send` envelope. Buffers until `ready` if needed. */
  send(
    prompt: string,
    conversationId?: string,
    tool?: {
      toolMode: string | null;
      toolParams: Record<string, unknown> | null;
      modelId?: string | null;
      /**
       * SSE-parity advanced fields (additive). Forwarded verbatim into the
       * `send` envelope so the WS data plane matches the SSE route's feature
       * set (sub-agent take-over + sampling overrides). `undefined` keys are
       * omitted so the envelope shape is unchanged for plain turns.
       */
      subagentId?: string | null;
      allowQuestion?: boolean;
      /** Main-agent turn only: let first-level sub-agents spawn grand
       *  sub-agents (forwarded as `allow_child_spawn`). */
      allowChildSpawn?: boolean;
      /** Sub-agent take-over only: let this sub-agent spawn sub-agents
       *  (forwarded as `self_allow_spawn`). */
      selfAllowSpawn?: boolean;
      temperature?: number | null;
      topP?: number | null;
      maxTokens?: number | null;
      /** Per-session ("this conversation only") tool / SKILL override —
       *  arrays of names switched OFF for this session. Omitted when empty. */
      disabledTools?: string[];
      disabledSkills?: string[];
      /** UI language (en / zh-CN / zh-TW) for system-prompt framing
       *  localization (forwarded as `locale`). Omitted when absent. */
      locale?: string | null;
    },
  ): void;
  /** Send a `stop` envelope. */
  stop(): void;
  /** Close the connection cleanly. */
  close(code?: number, reason?: string): void;
  /** Get the underlying socket (for tests / introspection). */
  rawSocket(): WebSocket | null;
}

// ---------------------------------------------------------------------------
// URL builder
// ---------------------------------------------------------------------------

function buildWsUrl(path: string, params?: ChatWsConnectParams | null): string {
  let resolved: string;
  if (/^wss?:\/\//i.test(path)) {
    resolved = path;
  } else {
    const base = wsBaseUrl();
    if (base === "") {
      resolved = path.startsWith("/") ? path : `/${path}`;
    } else {
      resolved = path.startsWith("/") ? `${base}${path}` : `${base}/${path}`;
    }
  }
  // Append the mandatory upgrade-time query params. The backend's WS route
  // declares `conversation_id` + `tab_id` as REQUIRED Query params; a bare
  // path is rejected with 403 before the handshake. Encode each value and
  // join with the existing query string if the caller's path already had one.
  if (params !== undefined && params !== null) {
    const qs =
      `conversation_id=${encodeURIComponent(params.conversationId)}` +
      `&tab_id=${encodeURIComponent(params.tabId)}`;
    resolved += (resolved.includes("?") ? "&" : "?") + qs;
  }
  return resolved;
}

// ---------------------------------------------------------------------------
// Type guards
// ---------------------------------------------------------------------------

function asServerMessage(raw: unknown): ChatWsServerMessage | null {
  if (raw === null || typeof raw !== "object") {
    return null;
  }
  const o = raw as Record<string, unknown>;
  const t = o["type"];
  if (typeof t !== "string") {
    return null;
  }
  if (t === "ready" && typeof o["session_id"] === "string") {
    return { type: "ready", session_id: o["session_id"] } as ChatWsReady;
  }
  if (t === "frame" && o["frame"] !== undefined) {
    return { type: "frame", frame: o["frame"] as ChatStreamFrame } as ChatWsFrame;
  }
  if (t === "error" && o["error"] !== undefined) {
    return {
      type: "error",
      error: o["error"] as ApiErrorPayload,
    } as ChatWsError;
  }
  if (t === "done") {
    return { type: "done" } as ChatWsDone;
  }
  if (t === "ping") {
    return { type: "ping" } as ChatWsPing;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

export function useChatWebSocket(
  options: UseChatWebSocketOptions = {},
): ChatWebSocketClient {
  const path = options.path ?? "/api/chat/ws";
  const maxReconnect = options.maxReconnect ?? 5;
  const backoffBaseMs = options.backoffBaseMs ?? 200;
  const ctor: typeof WebSocket =
    options.wsCtor ?? (globalThis.WebSocket as typeof WebSocket);

  let state: ChatWsState = "idle";
  let socket: WebSocket | null = null;
  let sessionId: string | null = null;
  let reconnectAttempt = 0;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let pending: ChatWsClientMessage | null = null;
  let handlers: ChatWsHandlers = {};
  let terminatedNormally = false;
  let manualClose = false;
  // Mid-turn-reconnect detection (root-cause fix for the "tab stalls silently
  // after a WS drop" bug). A turn lifecycle is:
  //   open() → ready → send → frame*  → done|error → server closes
  // If the socket dies AFTER `send` was flushed but BEFORE `done`/`error`
  // arrived, the server's `_run_one_turn` has already torn down the in-flight
  // turn (the next `send_json` raised WebSocketDisconnect, the generator was
  // GC'd, and a fresh WS upgrade is now a blank session). The auto-reconnect
  // here would then succeed at the TCP/handshake level — server promptly
  // emits a fresh `ready` — but `pending` is null (it was consumed on the
  // first ready) and there is nothing to re-send, so the new socket sits
  // idle forever and the UI tab freezes with no error feedback.
  //
  // We track whether at least one `send` envelope has been flushed since the
  // last `ready`, so that on a SUBSEQUENT `ready` (i.e. after reconnect) we
  // can detect "this is a reconnect, not a fresh open, and the prior turn
  // was unfinished" and surface it as a synthetic error envelope.  The
  // existing `onError` channel (recordError → tab.status="error" →
  // settleReject) is reused so the user sees a clean retryable error
  // instead of an indefinite stall.
  let sentSinceLastReady = false;

  function clearReconnectTimer(): void {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  }

  function dispatch(msg: ChatWsServerMessage): void {
    switch (msg.type) {
      case "ready":
        // Mid-turn-reconnect detection (root-cause fix for "tab stalls
        // silently after a WS drop"). If a prior `send` was flushed but no
        // `done`/`error` arrived before the socket died, the server has
        // already torn down the in-flight turn — a fresh `ready` on the
        // reconnected socket is a NEW blank session, not a resumption of
        // the old turn. `pending` is null (consumed on the first ready)
        // so the client cannot transparently re-send, and silently waiting
        // for frames that will never come freezes the tab indefinitely.
        // Surface a synthetic error envelope through the existing onError
        // channel so the transport's `handleErrorEnvelope` flips the tab
        // to "error" with a retryable, user-actionable diagnostic — the
        // user can then resend. We close the socket locally (manualClose)
        // so the close handler does not trigger another reconnect attempt.
        if (sentSinceLastReady) {
          sentSinceLastReady = false;
          terminatedNormally = true;
          handlers.onError?.({
            type: "NetworkError",
            code: "client.ws_reconnected_mid_turn",
            message: (
              "网络连接已重建，但上一次请求已被中断。请重新发送。"
            ),
          });
          manualClose = true;
          clearReconnectTimer();
          if (socket !== null) {
            state = "closing";
            try {
              socket.close(1000, "client mid-turn reconnect");
            } catch {
              // ignore
            }
          } else {
            state = "closed";
          }
          break;
        }
        sessionId = msg.session_id;
        state = "ready";
        reconnectAttempt = 0;
        handlers.onReady?.(msg.session_id);
        // Flush any queued send.
        if (pending !== null && socket !== null && socket.readyState === 1) {
          try {
            socket.send(JSON.stringify(pending));
            sentSinceLastReady = true;
          } catch {
            // ignore
          }
          pending = null;
        }
        break;
      case "frame":
        handlers.onFrame?.(msg.frame);
        break;
      case "error":
        terminatedNormally = true;
        sentSinceLastReady = false;
        handlers.onError?.(msg.error);
        break;
      case "done":
        terminatedNormally = true;
        sentSinceLastReady = false;
        handlers.onDone?.();
        break;
      case "ping":
        // Server keep-alive heartbeat (non-terminal). Pure liveness signal —
        // ignore entirely: do NOT touch `sentSinceLastReady` (so it can never
        // be mistaken for a mid-turn reconnect), do NOT change `state`, and do
        // NOT invoke any handler (no onFrame/onReady/onDone/onError). Its sole
        // effect is that receiving it proves the socket is alive, so the
        // intermediary idle-timeout that this heartbeat exists to defeat never
        // fires.
        break;
      default:
        // Unknown shape: ignore.
        break;
    }
  }

  function scheduleReconnect(): void {
    if (manualClose) {
      return;
    }
    if (reconnectAttempt >= maxReconnect) {
      handlers.onGiveUp?.();
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
      handlers.onGiveUp?.();
      return;
    }
    // Resolve the mandatory upgrade query params fresh on EVERY connect
    // (initial + reconnect) so a reconnect carries the same conversation_id
    // / tab_id. If the caller cannot supply them yet, do not open a
    // query-less URL the backend would 403 — give up so the transport falls
    // back instead of silently looping 403s.
    let connectParams: ChatWsConnectParams | null = null;
    if (options.getConnectParams !== undefined) {
      connectParams = options.getConnectParams();
      if (connectParams === null) {
        handlers.onGiveUp?.();
        return;
      }
    }
    state = "connecting";
    sessionId = null;
    terminatedNormally = false;
    let ws: WebSocket;
    try {
      ws = new ctor(buildWsUrl(path, connectParams));
    } catch {
      // Constructor threw — treat as failed connect attempt.
      scheduleReconnect();
      return;
    }
    socket = ws;
    ws.addEventListener("open", () => {
      // No-op until ready handshake arrives.
    });
    ws.addEventListener("message", (ev: MessageEvent) => {
      // Identity guard: ignore any late frame from a spent socket that
      // `open()` superseded (see the close handler) so it cannot leak into a
      // newly-opened turn.
      if (ws !== socket) {
        return;
      }
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
      const msg = asServerMessage(parsed);
      if (msg !== null) {
        dispatch(msg);
      }
    });
    ws.addEventListener("error", () => {
      // Close handler will run after; ignore here.
    });
    ws.addEventListener("close", (ev: CloseEvent) => {
      const code = ev.code;
      const reason = ev.reason;
      const wasClean = ev.wasClean;
      // Identity guard: only act if the socket that closed is STILL the
      // current one. When `open()` proactively closes a spent (terminated)
      // socket and immediately reconnects, this stale close event must NOT
      // null out / "closed"-flag the freshly-created socket (which would kill
      // the new turn). A stale close is a no-op.
      if (ws !== socket) {
        return;
      }
      socket = null;
      const wasReady = state === "ready" || state === "connecting";
      state = "closed";
      handlers.onClose?.({ code, reason, wasClean });
      if (manualClose || terminatedNormally) {
        // Don't reconnect after explicit close or after error/done.
        return;
      }
      if (wasReady) {
        scheduleReconnect();
      }
    });
  }

  function open(h: ChatWsHandlers): void {
    handlers = h;
    // A socket that already saw a terminal `done`/`error` for the PREVIOUS
    // turn is SPENT: the backend `_run_one_turn` loop closes the connection
    // right after sending `done`, so any `send` written to it now is silently
    // dropped (the server is no longer reading). This is the root cause of
    // "queue re-send produced no backend turn": the auto-dequeue re-send fired
    // on the `done`→idle edge while `state` was still "ready" (the close event
    // had not yet flipped it to "closed"), so the old early-return reused the
    // dying socket. Force a FRESH connection whenever the prior turn
    // terminated normally — close the spent socket first so its async close
    // event cannot later schedule a stray reconnect.
    if (terminatedNormally) {
      const spent = socket;
      socket = null;
      state = "closed";
      if (spent !== null) {
        try {
          spent.close(1000, "client reopen after turn end");
        } catch {
          // ignore — already closing/closed
        }
      }
    }
    if (state === "connecting" || state === "ready") {
      // Replace handlers but don't reopen.
      return;
    }
    manualClose = false;
    reconnectAttempt = 0;
    pending = null;
    sentSinceLastReady = false;
    terminatedNormally = false;
    doConnect();
  }

  function send(
    prompt: string,
    conversationId?: string,
    tool?: {
      toolMode: string | null;
      toolParams: Record<string, unknown> | null;
      modelId?: string | null;
      subagentId?: string | null;
      allowQuestion?: boolean;
      allowChildSpawn?: boolean;
      selfAllowSpawn?: boolean;
      temperature?: number | null;
      topP?: number | null;
      maxTokens?: number | null;
      disabledTools?: string[];
      disabledSkills?: string[];
      locale?: string | null;
    },
  ): void {
    const base: ChatWsClientMessage =
      conversationId !== undefined
        ? { type: "send", prompt, conversation_id: conversationId }
        : { type: "send", prompt };
    // Attach optional feature-mode fields (additive — never replaces the
    // existing send envelope shape).
    const modelId =
      tool !== undefined &&
      typeof tool.modelId === "string" &&
      tool.modelId !== ""
        ? tool.modelId
        : null;
    // SSE-parity advanced fields (additive). Only emit keys that are set so
    // the envelope is byte-identical to the prior shape for plain turns.
    const subagentId =
      tool !== undefined &&
      typeof tool.subagentId === "string" &&
      tool.subagentId !== ""
        ? tool.subagentId
        : null;
    const allowQuestion = tool?.allowQuestion === true;
    const allowChildSpawn = tool?.allowChildSpawn === true;
    const selfAllowSpawn = tool?.selfAllowSpawn === true;
    const temperature =
      tool !== undefined && typeof tool.temperature === "number"
        ? tool.temperature
        : null;
    const topP =
      tool !== undefined && typeof tool.topP === "number" ? tool.topP : null;
    const maxTokens =
      tool !== undefined &&
      typeof tool.maxTokens === "number" &&
      tool.maxTokens > 0
        ? tool.maxTokens
        : null;
    // Per-session tool / SKILL override (this conversation only; additive).
    // Only emitted when non-empty so the envelope is byte-identical to the
    // prior shape for sessions without an override.
    const disabledTools =
      tool !== undefined &&
      Array.isArray(tool.disabledTools) &&
      tool.disabledTools.length > 0
        ? tool.disabledTools
        : null;
    const disabledSkills =
      tool !== undefined &&
      Array.isArray(tool.disabledSkills) &&
      tool.disabledSkills.length > 0
        ? tool.disabledSkills
        : null;
    const locale =
      tool !== undefined &&
      typeof tool.locale === "string" &&
      tool.locale !== ""
        ? tool.locale
        : null;
    const hasAdvanced =
      tool !== undefined &&
      (tool.toolMode !== null ||
        tool.toolParams !== null ||
        modelId !== null ||
        subagentId !== null ||
        allowQuestion ||
        allowChildSpawn ||
        selfAllowSpawn ||
        temperature !== null ||
        topP !== null ||
        maxTokens !== null ||
        disabledTools !== null ||
        disabledSkills !== null ||
        locale !== null);
    const envelope: ChatWsClientMessage = hasAdvanced
      ? {
          ...base,
          ...(tool!.toolMode !== null ? { tool_mode: tool!.toolMode } : {}),
          ...(tool!.toolParams !== null
            ? { tool_params: tool!.toolParams }
            : {}),
          ...(modelId !== null ? { model_id: modelId } : {}),
          ...(subagentId !== null ? { subagent_id: subagentId } : {}),
          ...(allowQuestion ? { allow_question: true } : {}),
          ...(allowChildSpawn ? { allow_child_spawn: true } : {}),
          ...(selfAllowSpawn ? { self_allow_spawn: true } : {}),
          ...(temperature !== null ? { temperature } : {}),
          ...(topP !== null ? { top_p: topP } : {}),
          ...(maxTokens !== null ? { max_tokens: maxTokens } : {}),
          ...(disabledTools !== null ? { disabled_tools: disabledTools } : {}),
          ...(disabledSkills !== null
            ? { disabled_skills: disabledSkills }
            : {}),
          ...(locale !== null ? { locale } : {}),
        }
      : base;
    if (state === "ready" && socket !== null && socket.readyState === 1) {
      try {
        socket.send(JSON.stringify(envelope));
        sentSinceLastReady = true;
      } catch {
        // ignore — the close handler will retry
      }
      return;
    }
    pending = envelope;
  }

  function stop(): void {
    const envelope: ChatWsClientMessage = { type: "stop" };
    if (socket !== null && socket.readyState === 1) {
      try {
        socket.send(JSON.stringify(envelope));
      } catch {
        // ignore
      }
    }
  }

  function close(code?: number, reason?: string): void {
    manualClose = true;
    clearReconnectTimer();
    if (socket !== null) {
      state = "closing";
      try {
        if (code !== undefined) {
          socket.close(code, reason);
        } else {
          socket.close();
        }
      } catch {
        // ignore
      }
    } else {
      state = "closed";
    }
  }

  return {
    state: () => state,
    sessionId: () => sessionId,
    reconnectCount: () => reconnectAttempt,
    open,
    send,
    stop,
    close,
    rawSocket: () => socket,
  };
}
