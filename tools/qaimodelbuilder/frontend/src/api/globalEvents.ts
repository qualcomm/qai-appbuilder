// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Global Events SSE client — `connectGlobalEvents`.
 *
 * Connects to `GET /api/events` using the browser's native `EventSource`
 * API. This endpoint streams server-pushed notifications for:
 *   - `reboot`                   — server restart imminent
 *   - `service_started`          — inference daemon came up
 *   - `cc_session_updated`       — Claude Code session changed (channel sync)
 *   - `permission_request`       — file-access authorization needed
 *   - `wechat_update_conv`       — WeChat channel message sync
 *   - `feishu_update_conv`       — Feishu channel message sync
 *
 * Unlike the QAI-native SSE helper (`apiSSE`), this endpoint uses the
 * standard `EventSource` protocol with unnamed events (`onmessage`),
 * auto-reconnect, and JSON payloads discriminated by `{ type: ... }`.
 *
 * The helper returns a dispose function to close the connection.
 */

import { apiBaseUrl, wsBaseUrl } from "./base";

/**
 * A server-pushed global event from `GET /api/events`.
 *
 * `/api/events` is a raw `EventSource` stream of JSON payloads
 * discriminated by `type` (reboot / service_started / cc_session_updated
 * / permission_request / wechat_update_conv / feishu_update_conv / ...).
 * It has no single OpenAPI response schema, so the open union is defined
 * here in the frontend.
 */
export interface GlobalSseEvent {
  type: string;
  [key: string]: unknown;
}

/** Callback invoked for each parsed global event. */
export type GlobalEventHandler = (event: GlobalSseEvent) => void;

/** Options for `connectGlobalEvents`. */
export interface GlobalEventsOptions {
  /** Called for every parsed event. */
  readonly onEvent: GlobalEventHandler;
  /** Called when the EventSource connection opens. */
  readonly onOpen?: () => void;
  /** Called on EventSource errors (browser will auto-retry). */
  readonly onError?: (ev: Event) => void;
}

/**
 * Central registry of KNOWN NAMED SSE events on `/api/events`.
 *
 * The `/api/events` route emits NAMED SSE frames (`event: <event_type>`),
 * e.g. `event: channels.webui.inbound`. The browser's `EventSource` delivers
 * named frames to `addEventListener(name, ...)` — NOT to `onmessage` (which
 * only fires for the default unnamed `message` event). So every named frame
 * type MUST be listed here, otherwise `EventSource` SILENTLY DROPS it and the
 * frontend never sees the event.
 *
 * ⚠️ REGISTRATION CONTRACT: whenever the backend adds a new `DomainEvent`
 * `event_type` that is serialised as a NAMED SSE frame (i.e. published on the
 * shared EventBus and emitted by the `/api/events` route as
 * `event: <event_type>`), it MUST be registered here. Forgetting to add it
 * means the browser's `EventSource` will silently discard the frame and the
 * corresponding UI (sidebar refresh, background-process card, …) will freeze.
 * This constant is the SINGLE, VISIBLE source of truth for "which named events
 * do we listen for", and can be asserted against in tests.
 *
 * Current members:
 *   - `channels.webui.inbound` / `channels.webui.outbound` — channel WebUI
 *     live-update frames (their JSON payload carries a `type` discriminator,
 *     `wechat_update_conv` / `feishu_update_conv`, V1-aligned). Without these
 *     the sidebar history list never refreshed on a Feishu/WeChat message.
 *   - `background_process.updated` / `background_process.deleted` — the manager
 *     publishes these on the shared EventBus and the `/api/events` route
 *     serialises each as `event: background_process.updated`. Without them the
 *     in-conversation `BackgroundProcessCard` never receives real-time state
 *     transitions (starting → running → ready → exited) and freezes on the
 *     spawn-time snapshot — the "process stuck at 启动中 / PID —" bug. Their
 *     JSON payload's `type` field (set by `_serialise_event` to the
 *     event_type) drives the card's `applyUpdatedEvent` / `applyDeletedEvent`.
 *   - `security.permission_requested` — the backend `RequestPermissionUseCase`
 *     publishes this on the shared EventBus when a file-access / command needs
 *     interactive authorization; the `/api/events` route serialises it as
 *     `event: security.permission_requested` and its `.to_dict()` payload
 *     carries `type: "permission_request"` (+ id/op/path/caller) which
 *     `usePermissionDialog` consumes to pop the ASK dialog. Without it the
 *     browser silently drops the frame, the request sits PENDING for 60s and
 *     then fails closed — the "文件访问不弹授权窗口、直接返回安全策略拒绝"
 *     bug (a named-event registration regression; the ASK path itself is intact).
 *
 * All named frames are routed into the SAME `onEvent` callback as the unnamed
 * `onmessage` events, so subscribers discriminate purely on the JSON payload.
 */
export const KNOWN_NAMED_EVENTS = [
  "channels.webui.inbound",
  "channels.webui.outbound",
  "background_process.updated",
  "background_process.deleted",
  "security.permission_requested",
] as const;

// ---------------------------------------------------------------------------
// Module-level singleton + reference counting.
//
// A single browser tab needs at most ONE `EventSource` to `/api/events`;
// opening one per component (App shell + every BackgroundProcessCard) wastes
// connections and risks exhausting the browser's per-origin EventSource limit.
// We therefore keep at most ONE live `EventSource` and a `Set` of subscriber
// option bundles. Each subscriber's `onEvent` / `onOpen` / `onError` is called
// on every corresponding event (broadcast). The first subscriber lazily opens
// the connection; the dispose returned to each subscriber removes only ITS OWN
// bundle, and only when the set becomes empty do we actually `es.close()` and
// null the singleton (so a later subscribe transparently reopens it).
// ---------------------------------------------------------------------------

let _es: EventSource | null = null;
let _ws: WebSocket | null = null;
const _subscribers = new Set<GlobalEventsOptions>();

// Transport state machine. We PREFER WebSocket (browsers cap HTTP/1.1 +
// EventSource at ~6 connections per origin; WebSocket is exempt) and FALL
// BACK to the SSE ``/api/events`` route when the WS handshake fails. Only one
// transport is live at a time; both feed the identical ``_dispatchEvent`` +
// broadcast ``onOpen`` / ``onError`` so consumers are transport-agnostic.
let _closedByUs = false;
let _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let _reconnectAttempts = 0;
let _usingSseFallback = false;
// Whether the CURRENT WebSocket transport ever reached the OPEN state. Used by
// ``onclose`` to distinguish a first-attempt handshake failure (never opened →
// fall back to SSE) from a transient drop of a previously-open socket (→ keep
// WS as the preferred transport and reconnect with backoff). ``onopen`` resets
// ``_reconnectAttempts`` to 0, so that counter alone cannot tell the two apart.
let _wsEverOpened = false;
const _RECONNECT_BASE_MS = 500;
const _RECONNECT_MAX_MS = 15_000;

/** Broadcast a parsed event to every current subscriber's `onEvent`. */
function _dispatchEvent(msg: MessageEvent<string>): void {
  let parsed: GlobalSseEvent;
  try {
    parsed = JSON.parse(msg.data) as GlobalSseEvent;
  } catch {
    return; // malformed payload — skip silently
  }
  // Server keepalive sentinel (WS transport) — not a domain event.
  if (parsed && parsed.type === "__heartbeat__") {
    return;
  }
  // Snapshot to be safe if a handler disposes during iteration.
  for (const sub of [..._subscribers]) {
    sub.onEvent(parsed);
  }
}

function _broadcastOpen(): void {
  for (const sub of [..._subscribers]) {
    sub.onOpen?.();
  }
}

function _broadcastError(ev: Event): void {
  for (const sub of [..._subscribers]) {
    sub.onError?.(ev);
  }
}

/**
 * Schedule a reconnect with exponential backoff. Unlike ``EventSource`` (which
 * auto-reconnects for free), ``WebSocket`` does not, so the singleton owns the
 * retry loop. On every successful (re)open ``onOpen`` is re-fired to all
 * subscribers so ``App.vue``'s ``fetchPending()`` catch-up recovers any
 * permission requests pushed while disconnected (the one behaviour we must not
 * lose vs SSE).
 */
function _scheduleReconnect(): void {
  if (_closedByUs || _subscribers.size === 0 || _reconnectTimer !== null) {
    return;
  }
  const delay = Math.min(
    _RECONNECT_MAX_MS,
    _RECONNECT_BASE_MS * 2 ** Math.min(_reconnectAttempts, 5),
  );
  _reconnectAttempts += 1;
  _reconnectTimer = setTimeout(() => {
    _reconnectTimer = null;
    if (_closedByUs || _subscribers.size === 0) {
      return;
    }
    _ensureTransport();
  }, delay);
}

/** Open the SSE fallback transport (``/api/events``). */
function _openSse(): void {
  const base = apiBaseUrl();
  const url = base ? `${base}/api/events` : "/api/events";
  const es = new EventSource(url);
  _usingSseFallback = true;

  es.onopen = () => {
    _reconnectAttempts = 0;
    _broadcastOpen();
  };
  es.onmessage = (msg: MessageEvent<string>) => _dispatchEvent(msg);
  // SSE named frames must be registered explicitly or the browser drops them.
  for (const name of KNOWN_NAMED_EVENTS) {
    es.addEventListener(name, _dispatchEvent as EventListener);
  }
  // EventSource auto-reconnects internally, so we do NOT schedule our own
  // reconnect on SSE error — just surface it. (We only fall back to SSE after
  // WS failed; once on SSE we stay on SSE for this connection's lifetime.)
  es.onerror = (ev: Event) => _broadcastError(ev);
  _es = es;
}

/** Open the preferred WebSocket transport (``/api/ws/events``). */
function _openWs(): void {
  let ws: WebSocket;
  try {
    const base = wsBaseUrl();
    const url = base ? `${base}/api/ws/events` : "/api/ws/events";
    ws = new WebSocket(url);
  } catch {
    // Constructor threw (e.g. no window / bad URL) → fall back to SSE.
    _openSse();
    return;
  }

  ws.onopen = () => {
    _reconnectAttempts = 0;
    _wsEverOpened = true;
    _broadcastOpen();
  };
  ws.onmessage = (msg: MessageEvent<string>) => _dispatchEvent(msg);
  ws.onerror = (ev: Event) => _broadcastError(ev);
  ws.onclose = () => {
    _ws = null;
    if (_closedByUs || _subscribers.size === 0) {
      return;
    }
    // If the WS never opened at all on the FIRST attempt, fall back to SSE
    // (handshake unsupported / blocked). Otherwise reconnect the WS with
    // backoff (transient drop — WS remains the preferred transport).
    if (!_wsEverOpened && _reconnectAttempts === 0) {
      _openSse();
    } else {
      _scheduleReconnect();
    }
  };
  _ws = ws;
}

/** Lazily open the shared transport (WS preferred, SSE fallback). */
function _ensureTransport(): void {
  if (_ws || _es) {
    return;
  }
  _closedByUs = false;
  if (typeof WebSocket !== "undefined" && !_usingSseFallback) {
    _openWs();
  } else {
    _openSse();
  }
}

/** Tear down whichever transport is live + cancel any pending reconnect. */
function _closeTransport(): void {
  _closedByUs = true;
  if (_reconnectTimer !== null) {
    clearTimeout(_reconnectTimer);
    _reconnectTimer = null;
  }
  if (_ws) {
    try {
      _ws.close();
    } catch {
      /* ignore */
    }
    _ws = null;
  }
  if (_es) {
    _es.close();
    _es = null;
  }
  _reconnectAttempts = 0;
  _usingSseFallback = false;
  _wsEverOpened = false;
}

/**
 * Subscribe to the shared persistent global-events connection.
 *
 * Transport: WebSocket (``/api/ws/events``) is preferred — it is exempt from
 * the browser's ~6-per-origin HTTP/1.1 connection cap that limits
 * ``EventSource`` — with automatic fallback to the SSE ``/api/events`` route
 * when the WS handshake fails. The connection is a MODULE-LEVEL SINGLETON: the
 * first subscriber opens it, every later subscriber reuses the same transport,
 * and each subscriber's callbacks are invoked (broadcast) on every event.
 * Returns a `disconnect` function that removes ONLY this subscriber; the
 * underlying transport is closed only when the last subscriber disconnects
 * (and is transparently reopened on the next subscribe). WebSocket drops are
 * retried with exponential backoff, re-firing ``onOpen`` on every reopen so
 * ``fetchPending`` catch-up runs.
 *
 * Usage:
 * ```ts
 * const disconnect = connectGlobalEvents({
 *   onEvent(evt) {
 *     if (evt.type === "reboot") { ... }
 *   },
 * });
 * // Later:
 * disconnect();
 * ```
 */
export function connectGlobalEvents(opts: GlobalEventsOptions): () => void {
  _subscribers.add(opts);
  _ensureTransport();

  let disposed = false;
  return () => {
    if (disposed) {
      return; // idempotent — disposing twice is a no-op
    }
    disposed = true;
    _subscribers.delete(opts);
    if (_subscribers.size === 0) {
      _closeTransport();
    }
  };
}
